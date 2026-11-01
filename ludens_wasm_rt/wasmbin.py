"""Just enough of the wasm binary format to list a module's imports and exports."""

from __future__ import annotations

VALTYPES = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64", 0x7B: "v128", 0x70: "funcref", 0x6F: "externref"}
KINDS = {0: "func", 1: "table", 2: "memory", 3: "global", 4: "tag"}


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def u32(self) -> int:
        result = shift = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    def name(self) -> str:
        n = self.u32()
        s = self.data[self.pos:self.pos + n].decode()
        self.pos += n
        return s

    def limits(self) -> None:
        flags = self.u32()
        self.u32()
        if flags & 1:
            self.u32()


def parse(data: bytes) -> dict:
    """Return {"imports": [(module, name, kind, sig|None)], "exports": [(name, kind)]}.

    sig is (params, results) of valtype names for function imports."""
    if data[:4] != b"\0asm":
        raise ValueError("not a wasm module")
    r = _Reader(data, 8)
    types: list[tuple[list[str], list[str]]] = []
    imports: list[tuple] = []
    exports: list[tuple] = []
    while r.pos < len(data):
        sec, size = r.byte(), r.u32()
        end = r.pos + size
        if sec == 1:
            for _ in range(r.u32()):
                r.byte()  # 0x60
                params = [VALTYPES.get(r.byte(), "?") for _ in range(r.u32())]
                results = [VALTYPES.get(r.byte(), "?") for _ in range(r.u32())]
                types.append((params, results))
        elif sec == 2:
            for _ in range(r.u32()):
                mod, name, kind = r.name(), r.name(), r.byte()
                sig = None
                if kind == 0:
                    sig = types[r.u32()]
                elif kind == 1:
                    r.byte(); r.limits()
                elif kind == 2:
                    r.limits()
                elif kind == 3:
                    r.byte(); r.byte()
                else:
                    r.byte(); r.u32()
                imports.append((mod, name, KINDS.get(kind, "?"), sig))
        elif sec == 7:
            for _ in range(r.u32()):
                name, kind = r.name(), r.byte()
                r.u32()
                exports.append((name, KINDS.get(kind, "?")))
        r.pos = end
    return {"imports": imports, "exports": exports}
