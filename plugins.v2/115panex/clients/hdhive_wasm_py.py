"""
HDHive signedFetch WASM signer implemented in Python.

This replaces the previous Node.js helper. It uses wasmtime to load
hdh_security_bg.wasm directly inside the MoviePilot Python runtime.
"""
import base64
import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import wasmtime
except Exception:  # pragma: no cover - handled at runtime by caller
    wasmtime = None


class HDHiveWasmUnavailable(RuntimeError):
    pass


class _ByteArray(bytearray):
    def subarray(self, start: int, end: int):
        return _ByteArray(self[start:end])


class _Crypto:
    def getRandomValues(self, arr):
        data = os.urandom(len(arr))
        arr[:len(data)] = data
        return arr

    def randomFillSync(self, arr):
        return self.getRandomValues(arr)


class _Global:
    def __init__(self):
        self.crypto = _Crypto()
        self.msCrypto = self.crypto
        self.process = type("Process", (), {"versions": type("Versions", (), {"node": "python-wasmtime"})()})()


class HDHivePythonSigner:
    def __init__(self, wasm_path: str, user_agent: str, languages: str = "zh-CN,zh"):
        if wasmtime is None:
            raise HDHiveWasmUnavailable("缺少 Python 依赖 wasmtime，请确认插件 requirements.txt 已安装 wasmtime")
        self.wasm_path = str(wasm_path)
        self.user_agent = user_agent or ""
        self.languages = languages or "zh-CN,zh"
        self.heap = [None] * 1024 + [None, None, True, False]
        self.heap_next = len(self.heap)
        self.memory = None
        self.wasm: Dict[str, Any] = {}
        self.store = wasmtime.Store()
        self._instantiate()

    # ---------- heap/object helpers ----------
    def _add(self, obj: Any) -> int:
        if self.heap_next == len(self.heap):
            self.heap.append(len(self.heap) + 1)
        idx = self.heap_next
        self.heap_next = self.heap[idx]
        self.heap[idx] = obj
        return idx

    def _get(self, idx: int) -> Any:
        return self.heap[idx]

    def _take(self, idx: int) -> Any:
        obj = self.heap[idx]
        if idx >= 1028:
            self.heap[idx] = self.heap_next
            self.heap_next = idx
        return obj

    def _mem_read(self, ptr: int, length: int) -> bytes:
        return bytes(self.memory.read(self.store, ptr, ptr + length))

    def _mem_write(self, ptr: int, data: bytes) -> None:
        self.memory.write(self.store, data, ptr)

    def _get_string(self, ptr: int, length: int) -> str:
        return self._mem_read(ptr, length).decode("utf-8")

    def _read_i32(self, ptr: int) -> int:
        return int.from_bytes(self._mem_read(ptr, 4), "little", signed=True)

    def _pass_bytes(self, data: bytes) -> tuple[int, int]:
        malloc = self.wasm["__wbindgen_export2"]
        ptr = malloc(self.store, len(data), 1)
        self._mem_write(ptr, data)
        return ptr, len(data)

    def _pass_string(self, value: str) -> tuple[int, int]:
        return self._pass_bytes((value or "").encode("utf-8"))

    # ---------- import callbacks ----------
    def _obj_attr(self, obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _is_function(self, idx: int) -> int:
        return int(callable(self._get(idx)))

    def _is_object(self, idx: int) -> int:
        obj = self._get(idx)
        return int(obj is not None and not isinstance(obj, (str, int, float, bool)))

    def _is_string(self, idx: int) -> int:
        return int(isinstance(self._get(idx), str))

    def _is_undefined(self, idx: int) -> int:
        return int(self._get(idx) is None)

    def _throw(self, ptr: int, length: int):
        raise RuntimeError(self._get_string(ptr, length))

    def _call(self, a: int, b: int, c: int) -> int:
        fn = self._get(a)
        this = self._get(b)
        arg = self._get(c)
        if callable(fn):
            return self._add(fn(arg))
        return self._add(None)

    def _crypto(self, idx: int) -> int:
        return self._add(self._obj_attr(self._get(idx), "crypto"))

    def _mscrypto(self, idx: int) -> int:
        return self._add(self._obj_attr(self._get(idx), "msCrypto"))

    def _get_random_values(self, a: int, b: int) -> None:
        crypto = self._get(a) or _Crypto()
        arr = self._get(b)
        if hasattr(crypto, "getRandomValues"):
            crypto.getRandomValues(arr)
            return
        data = os.urandom(len(arr))
        arr[:len(data)] = data

    def _random_fill_sync(self, a: int, b: int) -> None:
        arr = self._take(b)
        data = os.urandom(len(arr))
        arr[:len(data)] = data
        self._add(arr)

    def _length(self, idx: int) -> int:
        obj = self._get(idx)
        try:
            return len(obj)
        except Exception:
            return 0

    def _new_with_length(self, length: int) -> int:
        return self._add(_ByteArray(length))

    def _subarray(self, idx: int, start: int, end: int) -> int:
        obj = self._get(idx)
        return self._add(_ByteArray(obj[start:end]))

    def _prototype_set_call(self, ptr: int, length: int, obj_idx: int) -> None:
        data = bytes(self._get(obj_idx))[:length]
        self._mem_write(ptr, data)

    def _cast_bytes(self, ptr: int, length: int) -> int:
        return self._add(_ByteArray(self._mem_read(ptr, length)))

    def _cast_string(self, ptr: int, length: int) -> int:
        return self._add(self._get_string(ptr, length))

    def _clone_ref(self, idx: int) -> int:
        return self._add(self._get(idx))

    def _drop_ref(self, idx: int) -> None:
        self._take(idx)

    def _require(self) -> int:
        def require(name: str):
            if name == "crypto":
                return _Crypto()
            return None
        return self._add(require)

    def _make_func(self, params, results, func):
        return wasmtime.Func(self.store, wasmtime.FuncType(params, results), func)

    def _instantiate(self) -> None:
        module = wasmtime.Module.from_file(self.store.engine, self.wasm_path)
        i32 = wasmtime.ValType.i32()
        imports = {
            "__wbg_Error_ef53bc310eb298a0": self._make_func([i32, i32], [i32], lambda p, l: self._add(RuntimeError(self._get_string(p, l)))),
            "__wbg___wbindgen_is_function_754e9f305ff6029e": self._make_func([i32], [i32], self._is_function),
            "__wbg___wbindgen_is_object_56732c2bc353f41d": self._make_func([i32], [i32], self._is_object),
            "__wbg___wbindgen_is_string_c236cabd84a4d769": self._make_func([i32], [i32], self._is_string),
            "__wbg___wbindgen_is_undefined_67b456be8673d3d7": self._make_func([i32], [i32], self._is_undefined),
            "__wbg___wbindgen_throw_1506f2235d1bdba0": self._make_func([i32, i32], [], self._throw),
            "__wbg_call_9c758de292015997": self._make_func([i32, i32, i32], [i32], self._call),
            "__wbg_crypto_38df2bab126b63dc": self._make_func([i32], [i32], self._crypto),
            "__wbg_getRandomValues_c44a50d8cfdaebeb": self._make_func([i32, i32], [], self._get_random_values),
            "__wbg_length_4a591ecaa01354d9": self._make_func([i32], [i32], self._length),
            "__wbg_msCrypto_bd5a034af96bcba6": self._make_func([i32], [i32], self._mscrypto),
            "__wbg_new_with_length_36a4998e27b014c5": self._make_func([i32], [i32], self._new_with_length),
            "__wbg_node_84ea875411254db1": self._make_func([i32], [i32], lambda idx: self._add(self._obj_attr(self._get(idx), "node"))),
            "__wbg_process_44c7a14e11e9f69e": self._make_func([i32], [i32], lambda idx: self._add(self._obj_attr(self._get(idx), "process"))),
            "__wbg_prototypesetcall_3249fc62a0fafa30": self._make_func([i32, i32, i32], [], self._prototype_set_call),
            "__wbg_randomFillSync_6c25eac9869eb53c": self._make_func([i32, i32], [], self._random_fill_sync),
            "__wbg_require_b4edbdcf3e2a1ef0": self._make_func([], [i32], self._require),
            "__wbg_static_accessor_GLOBAL_9d53f2689e622ca1": self._make_func([], [i32], lambda: self._add(_Global())),
            "__wbg_static_accessor_GLOBAL_THIS_a1a35cec07001a8a": self._make_func([], [i32], lambda: self._add(_Global())),
            "__wbg_static_accessor_SELF_4c59f6c7ea29a144": self._make_func([], [i32], lambda: 0),
            "__wbg_static_accessor_WINDOW_e70ae9f2eb052253": self._make_func([], [i32], lambda: 0),
            "__wbg_subarray_4aa221f6a4f5ab22": self._make_func([i32, i32, i32], [i32], self._subarray),
            "__wbg_versions_276b2795b1c6a219": self._make_func([i32], [i32], lambda idx: self._add(self._obj_attr(self._get(idx), "versions"))),
            "__wbindgen_cast_0000000000000001": self._make_func([i32, i32], [i32], self._cast_bytes),
            "__wbindgen_cast_0000000000000002": self._make_func([i32, i32], [i32], self._cast_string),
            "__wbindgen_object_clone_ref": self._make_func([i32], [i32], self._clone_ref),
            "__wbindgen_object_drop_ref": self._make_func([i32], [], self._drop_ref),
        }
        externs = []
        for imp in module.imports:
            if imp.module != "./hdh_security_bg.js" or imp.name not in imports:
                raise HDHiveWasmUnavailable(f"不支持的 WASM import: {imp.module}.{imp.name}")
            externs.append(imports[imp.name])
        instance = wasmtime.Instance(self.store, module, externs)
        exports = instance.exports(self.store)
        self.wasm = {name: exports[name] for name in exports}
        self.memory = self.wasm["memory"]

    def init(self) -> Dict[str, Any]:
        sp = self.wasm["__wbindgen_add_to_stack_pointer"](self.store, -16)
        try:
            self.wasm["init"](self.store, sp)
            p = self._read_i32(sp)
            l = self._read_i32(sp + 4)
            err = self._read_i32(sp + 8)
            has_err = self._read_i32(sp + 12)
            if has_err:
                raise self._take(err)
            client_pub = self._mem_read(p, l)
            self.wasm["__wbindgen_export4"](self.store, p, l, 1)
            fp = hashlib.sha256(f"{self.user_agent}|{self.languages}".encode("utf-8")).hexdigest()
            return {
                "client_pub": base64.b64encode(client_pub).decode("ascii"),
                "ua_fingerprint": fp,
                "ts": int(time.time() * 1000),
            }
        finally:
            self.wasm["__wbindgen_add_to_stack_pointer"](self.store, 16)

    def finalize(self, cid: str, server_pub_b64: str, kid: int = 1) -> None:
        sp = self.wasm["__wbindgen_add_to_stack_pointer"](self.store, -16)
        try:
            p1, l1 = self._pass_string(cid)
            server_pub = base64.b64decode(server_pub_b64)
            p2, l2 = self._pass_bytes(server_pub)
            self.wasm["finalizeHandshake"](self.store, sp, p1, l1, p2, l2, kid)
            err = self._read_i32(sp)
            has_err = self._read_i32(sp + 4)
            if has_err:
                raise self._take(err)
        finally:
            self.wasm["__wbindgen_add_to_stack_pointer"](self.store, 16)

    def sign(self, method: str, path: str, ts: str, nonce: str, body: bytes, user_id: str) -> str:
        sp = self.wasm["__wbindgen_add_to_stack_pointer"](self.store, -16)
        rp = rl = 0
        try:
            p1, l1 = self._pass_string((method or "GET").upper())
            p2, l2 = self._pass_string(path)
            p3, l3 = self._pass_string(ts)
            p4, l4 = self._pass_string(nonce)
            p5, l5 = self._pass_bytes(body or b"")
            p6, l6 = self._pass_string(str(user_id or "0"))
            self.wasm["signRequest"](self.store, sp, p1, l1, p2, l2, p3, l3, p4, l4, p5, l5, p6, l6)
            p = self._read_i32(sp)
            l = self._read_i32(sp + 4)
            err = self._read_i32(sp + 8)
            has_err = self._read_i32(sp + 12)
            if has_err:
                raise self._take(err)
            rp, rl = p, l
            return self._get_string(p, l)
        finally:
            self.wasm["__wbindgen_add_to_stack_pointer"](self.store, 16)
            if rp or rl:
                self.wasm["__wbindgen_export4"](self.store, rp, rl, 1)

    def sign_after_handshake(self, hs: Dict[str, Any], method: str, path: str, body: bytes, user_id: str) -> Dict[str, str]:
        self.finalize(str(hs.get("cid", "")), str(hs.get("server_pub", "")), 1)
        ts = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        sig = self.sign(method, path, ts, nonce, body or b"", str(user_id or "0"))
        return {"ts": ts, "nonce": nonce, "sig": sig, "cid": str(hs.get("cid", "")), "kid": "1"}

