import ast
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

ALLOWED_MODULES = {"re", "json", "string", "collections", "math", "datetime", "urllib.parse"}
BLOCKED_IMPORTS = {"os", "sys", "subprocess", "shutil", "io", "pathlib", "socket", "http", "importlib", "ctypes", "signal", "multiprocessing", "threading", "pickle", "shelve", "marshal", "code", "codeop", "compile", "compileall", "py_compile", "zipimport", "pkgutil", "runpy", "site", "builtins", "__builtin__"}

BLOCKED_BUILTINS = {"exec", "eval", "compile", "open", "input", "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr", "hasattr", "__import__", "breakpoint", "memoryview", "property", "super", "classmethod", "staticmethod"}


def validate_python_code(code: str) -> tuple:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split('.')[0]
                if module in BLOCKED_IMPORTS:
                    return False, f"禁止导入模块: {module}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module = node.module.split('.')[0]
                if module in BLOCKED_IMPORTS:
                    return False, f"禁止导入模块: {module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval", "compile"):
                return False, f"禁止使用 {node.func.id}()"

    has_evaluate = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate":
            has_evaluate = True
            break
    if not has_evaluate:
        return False, "必须定义 evaluate(content: str) -> dict 函数"

    return True, "OK"


def execute_in_sandbox(code: str, content: str, timeout: int = 10) -> dict:
    safe_builtins = {}
    import builtins
    for name in dir(builtins):
        if name.startswith('_'):
            continue
        if name in BLOCKED_BUILTINS:
            continue
        obj = getattr(builtins, name)
        if callable(obj) or isinstance(obj, type):
            safe_builtins[name] = obj
    safe_builtins['True'] = True
    safe_builtins['False'] = False
    safe_builtins['None'] = None

    exec_globals = {"__builtins__": safe_builtins}

    for mod_name in ALLOWED_MODULES:
        try:
            exec_globals[mod_name] = __import__(mod_name)
        except ImportError:
            pass

    try:
        exec(code, exec_globals)
    except Exception as e:
        return {"passed": False, "reason": f"代码定义错误: {str(e)}"}

    evaluate_fn = exec_globals.get("evaluate")
    if not evaluate_fn:
        return {"passed": False, "reason": "未定义 evaluate 函数"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(evaluate_fn, content)
        try:
            result = future.result(timeout=timeout)
            if not isinstance(result, dict):
                return {"passed": False, "reason": f"evaluate 函数必须返回 dict，实际返回: {type(result).__name__}"}
            if "passed" not in result:
                return {"passed": False, "reason": "返回的 dict 必须包含 'passed' 字段"}
            return result
        except FuturesTimeout:
            return {"passed": False, "reason": f"执行超时({timeout}s)"}
        except Exception as e:
            return {"passed": False, "reason": f"执行错误: {str(e)}"}
