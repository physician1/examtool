import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass
from flask import current_app

@dataclass
class RunResult:
    ok: bool
    compiled: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False


def _trim(text: str) -> str:
    limit = current_app.config["MAX_CODE_OUTPUT_BYTES"]
    encoded = text.encode("utf-8", errors="replace")[:limit]
    return encoded.decode("utf-8", errors="replace")


def _preexec_limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    except Exception:
        pass


def find_cpp_compiler() -> str | None:
    """Return a usable C++ compiler executable, preferring an explicit config."""
    configured = (current_app.config.get("CPP_COMPILER") or "").strip()
    if configured:
        # Accept an absolute path or a command discoverable on PATH.
        if Path(configured).exists():
            return configured
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    for candidate in ("g++", "g++.exe", "clang++", "clang++.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def compiler_status() -> tuple[bool, str]:
    compiler = find_cpp_compiler()
    if not compiler:
        return False, (
            "No C++ compiler was found. Install GCC/G++ (or Clang) and make sure it is on PATH, "
            "or run the included Docker image, which already installs g++."
        )
    try:
        proc = subprocess.run([compiler, "--version"], capture_output=True, text=True, timeout=5)
        first_line = (proc.stdout or proc.stderr or compiler).splitlines()[0]
        return proc.returncode == 0, first_line
    except Exception as exc:
        return False, f"Compiler was found at {compiler}, but could not be executed: {exc}"


def _binary_path(temp_dir: str) -> Path:
    # Windows compilers conventionally emit .exe; Linux/macOS do not.
    return Path(temp_dir) / ("program.exe" if os.name == "nt" else "program")


def _runner_env() -> dict[str, str]:
    """Build a minimal child-process environment.

    Do not pass Flask/database/admin secrets directly to student programs. The
    local runner is still only a prototype sandbox, but stripping inherited
    secrets removes an unnecessary exposure path.
    """
    compiler = find_cpp_compiler()
    compiler_dir = str(Path(compiler).resolve().parent) if compiler else ""
    base_path = os.environ.get("PATH", "")
    path_value = compiler_dir + (os.pathsep if compiler_dir and base_path else "") + base_path
    env = {
        "PATH": path_value,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    if os.name == "nt":
        # Windows runtime DLL/tool discovery may depend on these basics.
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
    else:
        env["HOME"] = "/tmp"
        env["TMPDIR"] = "/tmp"
    return env


def compile_cpp(code: str) -> tuple[bool, str, str | None]:
    if not current_app.config.get("ALLOW_UNSAFE_LOCAL_RUNNER"):
        return False, "Local compiler is disabled. Enable a production sandbox or set ALLOW_UNSAFE_LOCAL_RUNNER=1 for trusted development.", None

    compiler = find_cpp_compiler()
    if not compiler:
        return False, (
            "C++ compiler unavailable on this server. Install GCC/G++ (or Clang), make sure it is on PATH, "
            "then restart the Flask terminal. Docker users can rebuild the included image, which already installs g++."
        ), None

    temp_dir = tempfile.mkdtemp(prefix="cpp_exam_")
    src = Path(temp_dir) / "main.cpp"
    binary = _binary_path(temp_dir)
    src.write_text(code, encoding="utf-8")
    try:
        proc = subprocess.run(
            [compiler, "-std=c++17", "-O2", "-pipe", str(src), "-o", str(binary)],
            capture_output=True,
            text=True,
            timeout=8,
            cwd=temp_dir,
            env=_runner_env(),
            preexec_fn=_preexec_limits if os.name == "posix" else None,
        )
        if proc.returncode != 0:
            return False, _trim(proc.stderr or proc.stdout), temp_dir
        return True, _trim(proc.stderr), temp_dir
    except FileNotFoundError:
        return False, "The configured C++ compiler could not be started. Check CPP_COMPILER/PATH and restart the app.", temp_dir
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out.", temp_dir


def run_cpp(code: str, stdin: str = "") -> RunResult:
    compiled, compile_msg, temp_dir = compile_cpp(code)
    if not compiled or temp_dir is None:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return RunResult(False, False, stderr=compile_msg)
    binary = _binary_path(temp_dir)
    try:
        proc = subprocess.run(
            [str(binary)], input=stdin, capture_output=True, text=True, timeout=3,
            cwd=temp_dir, env=_runner_env(),
            preexec_fn=_preexec_limits if os.name == "posix" else None,
        )
        return RunResult(
            ok=(proc.returncode == 0), compiled=True,
            stdout=_trim(proc.stdout), stderr=_trim(proc.stderr), returncode=proc.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(False, True, stdout=_trim(exc.stdout or ""), stderr="Program timed out.", timed_out=True)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def normalize_output(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()


def grade_code(question, code: str):
    if not question.tests:
        return 0.0, "No automatic test cases configured; instructor review required."
    compiled, compile_msg, temp_dir = compile_cpp(code)
    if not compiled or temp_dir is None:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return 0.0, f"Compilation failed. {compile_msg}"

    binary = _binary_path(temp_dir)
    total_weight = sum(max(t.weight, 0.0) for t in question.tests) or 1.0
    passed_weight = 0.0
    passed = 0
    try:
        for test in question.tests:
            try:
                proc = subprocess.run(
                    [str(binary)], input=test.input_data or "", capture_output=True, text=True,
                    timeout=3, cwd=temp_dir, env=_runner_env(),
                    preexec_fn=_preexec_limits if os.name == "posix" else None,
                )
                if proc.returncode == 0 and normalize_output(proc.stdout) == normalize_output(test.expected_output):
                    passed += 1
                    passed_weight += max(test.weight, 0.0)
            except subprocess.TimeoutExpired:
                pass

        score = question.points * (passed_weight / total_weight)
        return round(score, 2), f"Passed {passed}/{len(question.tests)} automated test case(s)."
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
