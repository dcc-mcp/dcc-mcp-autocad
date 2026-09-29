"""COM transport: the post-write read-back actually reads back.

These tests use a fake AutoCAD whose only interesting property is that it
models **disk** separately from **session**. That distinction is the whole
point of the write contract: a document still open in AutoCAD looks identical
whether or not its save reached the file. Making `Save` a no-op is therefore
the one mutation that turns "reported success" into "read-back disagreed", and
every failure test here works by switching that off.

No COM is involved: `ComTransport._com_module` is patched, so this runs on
Linux and macOS exactly as on Windows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dcc_mcp_autocad.transports import com_transport
from dcc_mcp_autocad.transports.com_transport import ComTransport
from dcc_mcp_autocad.write_contract import WriteVerificationError

ACADVER = "25.1s (LMS Tech)"


class _FakeLayer:
    def __init__(self, name):
        self.Name = name


class _FakeLayers:
    def __init__(self, names):
        self._names = list(names)

    @property
    def Count(self):
        return len(self._names)

    def Item(self, index):
        return _FakeLayer(self._names[index])

    def Add(self, name):
        self._names.append(name)
        return _FakeLayer(name)


class _FakeLayouts:
    def __init__(self, names=("Model",)):
        self._names = list(names)

    @property
    def Count(self):
        return len(self._names)

    def Item(self, index):
        return _FakeLayer(self._names[index])


class _FakeEntity:
    def __init__(self, name="AcDbLine", layer="0", layer_assignable=True):
        self.EntityName = name
        self._layer = layer
        self.layer_assignable = layer_assignable

    @property
    def Layer(self):
        return self._layer

    @Layer.setter
    def Layer(self, value):
        # AutoCAD silently refuses a layer that does not exist; modelling that
        # is the only way to prove the read-back catches it.
        if self.layer_assignable:
            self._layer = value


class _FakeModelSpace:
    def __init__(self, entities=(), layer_assignable=True):
        self._entities = list(entities)
        self.layer_assignable = layer_assignable

    @property
    def Count(self):
        return len(self._entities)

    def Item(self, index):
        return self._entities[index]

    def _append(self, name):
        entity = _FakeEntity(name, layer_assignable=self.layer_assignable)
        self._entities.append(entity)
        return entity

    def AddLine(self, start, end):
        return self._append("AcDbLine")

    def AddPoint(self, position):
        return self._append("AcDbPoint")

    def AddCircle(self, center, radius):
        return self._append("AcDbCircle")

    def AddText(self, text, position, height):
        return self._append("AcDbText")


class _FakeState:
    """What one DWG actually contains on disk."""

    def __init__(self, entities=(), layers=("0",), layer_assignable=True):
        self.entities = list(entities)
        self.layers = list(layers)
        self.layer_assignable = layer_assignable

    def copy(self):
        return _FakeState(
            [
                _FakeEntity(entity.EntityName, entity.Layer, layer_assignable=self.layer_assignable)
                for entity in self.entities
            ],
            list(self.layers),
            layer_assignable=self.layer_assignable,
        )


class _FakeDocument:
    def __init__(self, store, path, state, saves_succeed=True):
        self._store = store
        self._saves_succeed = saves_succeed
        self.path = path
        self.ModelSpace = _FakeModelSpace(state.entities, layer_assignable=state.layer_assignable)
        self.Layers = _FakeLayers(state.layers)
        self.Layouts = _FakeLayouts()

    def _snapshot(self):
        return _FakeState(
            [
                _FakeEntity(entity.EntityName, entity.Layer, layer_assignable=True)
                for entity in self.ModelSpace._entities
            ],
            list(self.Layers._names),
            layer_assignable=True,
        )

    def Save(self):
        if self._saves_succeed and self.path is not None:
            self._store[self.path] = self._snapshot()
            self._write_to_disk()

    def SaveAs(self, path):
        self.path = str(path)
        if self._saves_succeed:
            self._store[self.path] = self._snapshot()
            self._write_to_disk()

    def _write_to_disk(self):
        # The transport checks the file on the real filesystem, so the fake has
        # to leave one behind; a store-only save would make the read-back
        # untestable rather than passing.
        Path(self.path).write_bytes(b"AC1032 fake dwg")

    def Close(self, save_changes=False):
        return None

    def GetVariable(self, name):
        if name == "DWGNAME":
            return self.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if self.path else ""
        if name == "DWGPREFIX":
            return self.path or ""
        raise RuntimeError("unexpected variable %s" % name)


class _FakeDocuments:
    def __init__(self, store, saves_succeed=True):
        self._store = store
        self._saves_succeed = saves_succeed

    def Open(self, path):
        key = str(path)
        state = self._store.get(key)
        if state is None:
            raise RuntimeError("drawing not found: %s" % key)
        return _FakeDocument(self._store, key, state.copy(), saves_succeed=self._saves_succeed)

    def Add(self, template=None):
        return _FakeDocument(self._store, None, _FakeState(), saves_succeed=self._saves_succeed)


class _FakeApplication:
    def __init__(self, store, saves_succeed=True):
        self.Version = ACADVER
        self.Visible = False
        self.HWND = 1
        self.Documents = _FakeDocuments(store, saves_succeed=saves_succeed)


class _FakeClient:
    @staticmethod
    def VARIANT(vt, value):
        return value


@pytest.fixture
def fake_com(monkeypatch):
    """Install a fake AutoCAD; returns the on-disk store keyed by path."""
    monkeypatch.setattr(com_transport, "_settle", lambda seconds=None: None)
    monkeypatch.setattr(com_transport, "_com_module", lambda: (None, _FakeClient))

    def make(saves_succeed=True, initial=None):
        store = dict(initial or {})
        transport = ComTransport()
        transport._app = _FakeApplication(store, saves_succeed=saves_succeed)
        transport._initialised = True
        return transport, store

    return make


def _drawing(entity_count=1, layers=("0",)):
    state = _FakeState(layers=list(layers))
    for _ in range(entity_count):
        state.entities.append(_FakeEntity())
    return state


def test_add_entities_reports_the_persisted_count(fake_com, tmp_path):
    dwg = tmp_path / "a.dwg"
    transport, store = fake_com(initial={str(dwg): _drawing(entity_count=1)})

    result = transport.add_entities(
        str(dwg),
        [{"type": "line", "start": [0, 0], "end": [1, 1]}, {"type": "point", "position": [2, 2]}],
    )

    assert result["verified"] is True
    assert result["entity_count_after"] == 3
    assert result["entity_count_before"] == 1
    assert len(store[str(dwg)].entities) == 3, "the change must reach disk"


def test_add_entities_fails_when_the_save_is_dropped(fake_com, tmp_path):
    """The core case: AutoCAD raised no error, but nothing reached the file."""
    dwg = tmp_path / "a.dwg"
    transport, _ = fake_com(saves_succeed=False, initial={str(dwg): _drawing(entity_count=1)})

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.add_entities(str(dwg), [{"type": "line", "start": [0, 0], "end": [1, 1]}])

    assert excinfo.value.check == "entity_count_persisted"
    assert excinfo.value.expected == 2
    assert excinfo.value.actual == 1
    assert excinfo.value.host_version == ACADVER
    assert "expected 2" in str(excinfo.value)
    assert "read back 1" in str(excinfo.value)


def test_add_entities_fails_when_the_layer_did_not_stick(fake_com, tmp_path):
    """AutoCAD drops a layer assignment to a layer that does not exist."""
    dwg = tmp_path / "a.dwg"
    state = _drawing(entity_count=0)
    state.layer_assignable = False
    transport, _ = fake_com(initial={str(dwg): state})

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.add_entities(
            str(dwg), [{"type": "line", "start": [0, 0], "end": [1, 1]}], layer="WALLS"
        )

    assert excinfo.value.check == "entity_layer_persisted"


def test_manage_layers_verifies_persistence(fake_com, tmp_path):
    dwg = tmp_path / "a.dwg"
    transport, store = fake_com(initial={str(dwg): _drawing(entity_count=0)})

    result = transport.manage_layers(str(dwg), ["WALLS"])

    assert result["verified"] is True
    assert "WALLS" in result["layers"]
    assert "WALLS" in store[str(dwg)].layers


def test_manage_layers_fails_when_the_save_is_dropped(fake_com, tmp_path):
    """Regression: `created` was assembled from the arguments, not the DWG."""
    dwg = tmp_path / "a.dwg"
    transport, _ = fake_com(saves_succeed=False, initial={str(dwg): _drawing(entity_count=0)})

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.manage_layers(str(dwg), ["WALLS"])

    assert excinfo.value.check == "layers_persisted"
    assert excinfo.value.expected == ["WALLS"]
    assert excinfo.value.actual == ["0"]


def test_create_drawing_reads_back_the_new_file(fake_com, tmp_path):
    transport, store = fake_com()
    target = tmp_path / "new.dwg"

    summary = transport.create_drawing(str(target))

    assert summary.entity_count == 0
    assert summary.layers == ["0"]
    assert str(target) in store, "the drawing must exist on disk"


def test_create_drawing_fails_when_save_as_is_dropped(fake_com, tmp_path):
    transport, _ = fake_com(saves_succeed=False)
    target = tmp_path / "new.dwg"

    with pytest.raises(WriteVerificationError) as excinfo:
        transport.create_drawing(str(target))

    assert excinfo.value.check == "drawing_saved"
    assert excinfo.value.expected["exists"] is True
    assert excinfo.value.actual["exists"] is False


def test_read_back_reopens_from_disk_not_from_the_session(fake_com, tmp_path):
    """The read-back is a second Open; the open document proves nothing."""
    dwg = tmp_path / "a.dwg"
    transport, _ = fake_com(initial={str(dwg): _drawing(entity_count=1)})
    opened = []
    original = transport.application.Documents.Open

    def spy(path):
        opened.append(str(path))
        return original(path)

    transport.application.Documents.Open = spy

    transport.add_entities(str(dwg), [{"type": "point", "position": [0, 0]}])

    assert opened == [str(dwg), str(dwg)], "a mutation must be followed by a re-open"
