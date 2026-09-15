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


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _trim(text) -> str:
    """
    Limit how much compiler/program output is returned to the browser.
    This prevents a student program from producing enormous output.
    """
    if text is None:
        return ""

    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")

    text = str(text)

    limit = current_app.config["MAX_CODE_OUTPUT_BYTES"]
    encoded = text.encode("utf-8", errors="replace")[:limit]

    return encoded.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

def _compile_limits():
    """
    Limits used while GCC/G++ is compiling student code.

    IMPORTANT:
    We deliberately DO NOT set RLIMIT_NPROC here.

    g++ needs to launch helper processes such as:

        cc1plus
        as
        ld

    Restricting the process count during compilation caused errors such as:

        g++: fatal error: cannot execute 'as':
        posix_spawnp: Resource temporarily unavailable
    """
    try:
        import resource

        # Allow enough CPU time for ordinary C++ compilation.
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (15, 15)
        )

        # Give the compiler more memory than student programs.
        #
        # 384 MB is generally enough for normal undergraduate C++ code
        # while still placing a limit on runaway compiler memory usage.
        compile_memory = 384 * 1024 * 1024

        resource.setrlimit(
            resource.RLIMIT_AS,
            (compile_memory, compile_memory)
        )

        # Prevent compilation from creating excessively large files.
        compile_file_limit = 10 * 1024 * 1024

        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (compile_file_limit, compile_file_limit)
        )

        # DO NOT SET RLIMIT_NPROC HERE.
        # g++ must be able to spawn cc1plus, as and ld.

    except Exception:
        # Resource limits are an additional protection.
        # If the OS does not support them, compilation can still proceed.
        pass


def _runtime_limits():
    """
    Strict limits applied to the student's COMPILED PROGRAM.

    These limits protect the BeaconCode server from programs that:
      - run forever
      - consume excessive memory
      - create many processes
      - create huge files
    """
    try:
        import resource

        # Maximum CPU time for student code.
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (3, 3)
        )

        # Maximum virtual memory available to student code.
        runtime_memory = 256 * 1024 * 1024

        resource.setrlimit(
            resource.RLIMIT_AS,
            (runtime_memory, runtime_memory)
        )

        # Prevent the student program from writing enormous files.
        runtime_file_limit = 2 * 1024 * 1024

        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (runtime_file_limit, runtime_file_limit)
        )

        # Protect against fork bombs / excessive process creation.
        #
        # This restriction is intentionally kept for STUDENT programs,
        # but is not used when running g++.
        resource.setrlimit(
            resource.RLIMIT_NPROC,
            (32, 32)
        )

    except Exception:
        pass


# ---------------------------------------------------------------------------
# Compiler discovery
# ---------------------------------------------------------------------------

def find_cpp_compiler() -> str | None:
    """
    Return a usable C++ compiler executable.

    Priority:
      1. CPP_COMPILER configured by BeaconCode
      2. g++
      3. clang++
    """

    configured = (
        current_app.config.get("CPP_COMPILER") or ""
    ).strip()

    if configured:

        # Explicit absolute path such as:
        # /usr/bin/g++
        # C:/msys64/ucrt64/bin/g++.exe
        if Path(configured).exists():
            return configured

        # Or a command discoverable through PATH.
        resolved = shutil.which(configured)

        if resolved:
            return resolved

    for candidate in (
        "g++",
        "g++.exe",
        "clang++",
        "clang++.exe",
    ):
        resolved = shutil.which(candidate)

        if resolved:
            return resolved

    return None


def compiler_status() -> tuple[bool, str]:
    """
    Check whether the configured compiler exists and can execute.
    """

    compiler = find_cpp_compiler()

    if not compiler:
        return False, (
            "No C++ compiler was found. "
            "Install GCC/G++ (or Clang) and make sure it is on PATH, "
            "or use the included Docker image, which installs g++."
        )

    try:

        proc = subprocess.run(
            [compiler, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        output = proc.stdout or proc.stderr or compiler
        first_line = output.splitlines()[0]

        return proc.returncode == 0, first_line

    except subprocess.TimeoutExpired:

        return False, (
            f"Compiler exists at {compiler}, "
            "but checking its version timed out."
        )

    except Exception as exc:

        return False, (
            f"Compiler was found at {compiler}, "
            f"but could not be executed: {exc}"
        )


# ---------------------------------------------------------------------------
# Executable paths
# ---------------------------------------------------------------------------

def _binary_path(temp_dir: str) -> Path:
    """
    Return the compiled executable path.

    Windows uses program.exe.
    Linux/Render uses program.
    """

    if os.name == "nt":
        return Path(temp_dir) / "program.exe"

    return Path(temp_dir) / "program"


# ---------------------------------------------------------------------------
# Child-process environment
# ---------------------------------------------------------------------------

def _runner_env() -> dict[str, str]:
    """
    Build a minimal environment for compiler and student processes.

    We intentionally avoid exposing Flask/database/admin secrets to
    student programs.
    """

    compiler = find_cpp_compiler()

    compiler_dir = (
        str(Path(compiler).resolve().parent)
        if compiler
        else ""
    )

    base_path = os.environ.get("PATH", "")

    if compiler_dir and base_path:
        path_value = compiler_dir + os.pathsep + base_path
    elif compiler_dir:
        path_value = compiler_dir
    else:
        path_value = base_path

    env = {
        "PATH": path_value,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }

    if os.name == "nt":

        # Windows compilers may require these values.
        for key in (
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
        ):
            if key in os.environ:
                env[key] = os.environ[key]

    else:

        # Give Linux programs a harmless temporary home.
        env["HOME"] = "/tmp"
        env["TMPDIR"] = "/tmp"

    return env


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def compile_cpp(code: str) -> tuple[bool, str, str | None]:
    """
    Compile C++ source code.

    Returns:

        success
        compiler message
        temporary directory
    """

    if not current_app.config.get(
        "ALLOW_UNSAFE_LOCAL_RUNNER"
    ):
        return (
            False,
            (
                "Local compiler is disabled. "
                "Enable a production sandbox or set "
                "ALLOW_UNSAFE_LOCAL_RUNNER=1."
            ),
            None,
        )

    compiler = find_cpp_compiler()

    if not compiler:
        return (
            False,
            (
                "C++ compiler unavailable on this server. "
                "Install GCC/G++ (or Clang), make sure it is "
                "available on PATH, and restart the application."
            ),
            None,
        )

    temp_dir = tempfile.mkdtemp(
        prefix="cpp_exam_"
    )

    src = Path(temp_dir) / "main.cpp"
    binary = _binary_path(temp_dir)

    src.write_text(
        code,
        encoding="utf-8"
    )

    try:

        compile_command = [
            compiler,

            # Use C++17 for the course.
            "-std=c++17",

            # Disable optimization.
            #
            # Optimization is unnecessary for assessment programs
            # and increases compilation resource usage.
            "-O0",

            # Prevent ANSI color sequences appearing in the web UI.
            "-fno-diagnostics-color",

            str(src),

            "-o",
            str(binary),
        ]

        proc = subprocess.run(
            compile_command,

            capture_output=True,
            text=True,

            # Separate wall-clock timeout from RLIMIT_CPU.
            timeout=15,

            cwd=temp_dir,

            env=_runner_env(),

            # Compiler gets its own limits WITHOUT RLIMIT_NPROC.
            preexec_fn=(
                _compile_limits
                if os.name == "posix"
                else None
            ),
        )

        if proc.returncode != 0:

            message = (
                proc.stderr
                or proc.stdout
                or "Compilation failed."
            )

            return (
                False,
                _trim(message),
                temp_dir,
            )

        # Some compilers may emit warnings to stderr even when
        # compilation succeeds.
        compiler_message = _trim(
            proc.stderr or ""
        )

        return (
            True,
            compiler_message,
            temp_dir,
        )

    except FileNotFoundError:

        return (
            False,
            (
                "The configured C++ compiler could not be started. "
                "Check CPP_COMPILER/PATH and redeploy the app."
            ),
            temp_dir,
        )

    except subprocess.TimeoutExpired:

        return (
            False,
            (
                "Compilation timed out. "
                "The submitted program may be unusually large "
                "or the server may currently be under heavy load."
            ),
            temp_dir,
        )

    except Exception as exc:

        return (
            False,
            f"Unexpected compiler error: {exc}",
            temp_dir,
        )


# ---------------------------------------------------------------------------
# Compile + run
# ---------------------------------------------------------------------------

def run_cpp(
    code: str,
    stdin: str = "",
) -> RunResult:
    """
    Compile and execute C++ code using student runtime limits.
    """

    compiled, compile_msg, temp_dir = compile_cpp(code)

    if not compiled or temp_dir is None:

        if temp_dir:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )

        return RunResult(
            ok=False,
            compiled=False,
            stderr=compile_msg,
        )

    binary = _binary_path(temp_dir)

    try:

        proc = subprocess.run(
            [str(binary)],

            input=stdin,

            capture_output=True,
            text=True,

            # Wall-clock limit.
            timeout=3,

            cwd=temp_dir,

            env=_runner_env(),

            # Student executable gets STRICT limits.
            preexec_fn=(
                _runtime_limits
                if os.name == "posix"
                else None
            ),
        )

        return RunResult(
            ok=(proc.returncode == 0),
            compiled=True,
            stdout=_trim(proc.stdout),
            stderr=_trim(proc.stderr),
            returncode=proc.returncode,
        )

    except subprocess.TimeoutExpired as exc:

        return RunResult(
            ok=False,
            compiled=True,
            stdout=_trim(exc.stdout or ""),
            stderr="Program timed out.",
            timed_out=True,
        )

    except Exception as exc:

        return RunResult(
            ok=False,
            compiled=True,
            stderr=f"Program execution failed: {exc}",
        )

    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )


# ---------------------------------------------------------------------------
# Output comparison
# ---------------------------------------------------------------------------

def normalize_output(text: str) -> str:
    """
    Normalize program output before comparing it with a hidden test case.

    Trailing spaces on individual lines are ignored.
    Leading/internal whitespace is preserved.
    """

    if text is None:
        return ""

    return "\n".join(
        line.rstrip()
        for line in text.strip().splitlines()
    ).strip()


# ---------------------------------------------------------------------------
# Automatic grading
# ---------------------------------------------------------------------------

def grade_code(question, code: str):
    """
    Compile the student's submission once, then execute it against
    every hidden test case.

    Returns:

        score
        grading message
    """

    if not question.tests:

        return (
            0.0,
            (
                "No automatic test cases configured; "
                "instructor review required."
            ),
        )

    compiled, compile_msg, temp_dir = compile_cpp(code)

    if not compiled or temp_dir is None:

        if temp_dir:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )

        return (
            0.0,
            f"Compilation failed. {compile_msg}",
        )

    binary = _binary_path(temp_dir)

    total_weight = sum(
        max(test.weight, 0.0)
        for test in question.tests
    )

    if total_weight <= 0:
        total_weight = 1.0

    passed_weight = 0.0
    passed = 0

    try:

        for test in question.tests:

            try:

                proc = subprocess.run(
                    [str(binary)],

                    input=test.input_data or "",

                    capture_output=True,
                    text=True,

                    timeout=3,

                    cwd=temp_dir,

                    env=_runner_env(),

                    # Hidden tests execute STUDENT code,
                    # therefore runtime restrictions apply.
                    preexec_fn=(
                        _runtime_limits
                        if os.name == "posix"
                        else None
                    ),
                )

                actual = normalize_output(
                    proc.stdout
                )

                expected = normalize_output(
                    test.expected_output
                )

                if (
                    proc.returncode == 0
                    and actual == expected
                ):
                    passed += 1

                    passed_weight += max(
                        test.weight,
                        0.0,
                    )

            except subprocess.TimeoutExpired:

                # Timed-out test simply does not receive credit.
                pass

            except Exception:

                # A failed test process should not crash grading
                # for the entire submission.
                pass

        score = question.points * (
            passed_weight / total_weight
        )

        score = round(
            score,
            2,
        )

        return (
            score,
            (
                f"Passed {passed}/"
                f"{len(question.tests)} "
                "automated test case(s)."
            ),
        )

    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )
