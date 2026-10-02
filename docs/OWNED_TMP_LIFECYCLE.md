# Owned TMP lifecycle audit / fixture evidence

This task performs no existing TMP mass cleanup, runtime replacement or persistent
artifact deletion. The audit starts from verified origin/main. System TMP is
read only; tests allocate small new isolated fixtures only.

## Lifecycle and classes

| Producer | Class | Success / failure | Interruption / crash |
|---|---|---|---|
| CLI extraction/OCR `OwnedWorkspace` | owned ephemeral | context closes exact inode/UUID receipt | supervisor forwards SIGINT/SIGTERM; lease prevents live-child stale cleanup; next startup reaps only validated dead owned paths |
| PDF image rasterization/exporter | owned ephemeral | OwnedWorkspace context, direct CLI supervisor | outer owned TMP contains child scratch; stale recovery after hard kill |
| Core managed subprocess trees | owned ephemeral scratch | existing process registry stops owned descendants; inherited exact lease | existing registry signal cleanup; supervisor grace period and live lease |
| Test suites/native compiler probes | owned ephemeral | `scripts/test_suite.sh` / owned runner; compiler scratch in `$TMPDIR` | same supervisor/next-start recovery |
| Atomic JSON/CDM/checkpoint writes beside project | persistent transaction, not system TMP | atomic replace + finally unlink exact temporary file | preserves project/recovery data; no stale project deletion |
| Fifteen historical `hybrid_*`/`visual_recheck` diagnostic scripts | retained evidence, explicit opt-in | require absolute `BOOKTEXT_DIAGNOSTIC_DIR` outside system TMP before tools run | evidence deliberately persistent, never registered/deleted as owned TMP |
| Application packaging / project history / resumable chunk artifacts | persistent / explicitly retained | fixed explicit build/project destination | not TMP cleanup candidates |

Standalone historical diagnostics retain outputs only when explicitly requested.
No old `/tmp/booktext-*` file is adopted or removed.

## Ownership authority and bound

Private per-user namespace, random workspace UUID, schema/app/namespace token,
exact canonical path and device/inode receipt; private directory owner/mode;
non-following lease and marker reads. A PID/lease/open-file check retains active
or uninspectable objects. Recovery considers only entries in this authenticated
namespace, never a global prefix/age search. Legacy unknown objects are not
adopted. Missing lsof, malformed receipts and substitutions retain fail closed.
At 32 retained/live/unknown workspace slots, new work fails closed rather than
creating another workspace. Completed cycles leave no run directories/data;
constant small namespace metadata remains. This is a workspace count guard,
not a size cap for a legitimately active task or explicitly retained evidence.
SIGKILL cannot execute finally; the next safe launch performs owned recovery.

## Controlled test matrix

Fixture subprocesses allocate 8 KiB payloads. Tests cover context error/cancel,
SIGINT/SIGTERM, killed supervisor with live surviving leased worker, later stale
recovery, unknown names, symlinks/substituted inode/marker, busy foreign FD,
unavailable lsof, repeated success/failure cycles, and exhausted namespace.
Eight alternating success/failure cycles: owned before 0 directories / 0 payload
bytes; after every cycle 0 / 0. Actual CLI entrypoint success/invalid-input cycles
are also checked. No full model/application candidate is built.

## Audited source locations

All matches below were examined. Atomic persistent writes and historical retained
evidence are distinguished above; imports alone do not allocate TMP. Test
fixture TemporaryDirectory uses are contained by the canonical test runner.

```text
tools/hybrid_resolver_v7.py:15:import subprocess
tools/hybrid_resolver_v7.py:302:    subprocess.run(
tools/visual_recheck_v18.py:12:import subprocess
tools/visual_recheck_v18.py:34:    p = subprocess.run(
tools/visual_recheck_v18.py:138:                p = subprocess.run(
tools/owned_workspace.py:16:import subprocess
tools/owned_workspace.py:18:import tempfile
tools/owned_workspace.py:74:        result = subprocess.run([tool, "-nP", "-F", "p", "+D", str(path)], capture_output=True, text=True, timeout=5)
tools/owned_workspace.py:75:    except (OSError, subprocess.TimeoutExpired):
tools/owned_workspace.py:84:    base = Path(base or os.environ.get(f"{APP.upper()}_OWNED_TMP_BASE") or tempfile.gettempdir()).resolve()
tools/owned_workspace.py:236:        child = subprocess.Popen(command, env=env, start_new_session=True, pass_fds=(workspace._lease,))
tools/owned_workspace.py:252:                    except subprocess.TimeoutExpired:
tools/extract_native_text.py:8:import subprocess
tools/extract_native_text.py:54:    result = subprocess.run(
tools/extract_native_text.py:64:    info = subprocess.run(
tools/extract_native_text.py:75:    return subprocess.run(
tools/extract_native_text.py:82:    return int(subprocess.run(
tools/extract_native_text.py:88:    return subprocess.run(
tools/extract_native_text.py:143:    except (ValueError, subprocess.CalledProcessError) as error:
booktext.py:11:import subprocess
booktext.py:53:        stdout=subprocess.PIPE if capture else None,
booktext.py:54:        stderr=subprocess.PIPE if capture else None,
booktext.py:61:    result = subprocess.CompletedProcess(command, child.process.returncode, stdout, stderr)
booktext.py:63:        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
booktext.py:1225:    except subprocess.TimeoutExpired as error:
booktext.py:1371:    except subprocess.CalledProcessError:
booktext.py:1983:            except (OSError, ValueError, subprocess.CalledProcessError) as error:
tools/chunk_lifecycle.py:15:import tempfile
tools/chunk_lifecycle.py:44:    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
tools/hybrid_adjudicate_v4.py:13:import subprocess
tools/hybrid_adjudicate_v4.py:15:import tempfile
tools/hybrid_adjudicate_v4.py:25:    p = subprocess.run(
tools/hybrid_adjudicate_v4.py:102:    subprocess.run(
tools/machine_protocol.py:16:import subprocess
tools/machine_protocol.py:431:    child = ManagedProcess(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
tools/machine_protocol.py:646:        except (MachineError, ValueError, OSError, subprocess.SubprocessError) as error:
tools/machine_protocol.py:916:    except (ValueError, OSError, subprocess.SubprocessError) as error:
tools/hybrid_context_v5.py:13:import subprocess
tools/hybrid_context_v5.py:28:    p = subprocess.run(
tools/hybrid_context_v5.py:75:    subprocess.run(
tools/inspect_photo_set.py:12:import subprocess
tools/inspect_photo_set.py:30:    result = subprocess.run(command, check=True, text=True, capture_output=True)
tools/hybrid_production_v12.py:12:import subprocess
tools/hybrid_production_v12.py:27:    p = subprocess.run(
tools/diff_review_rtf.py:8:import subprocess
tools/diff_review_rtf.py:13:    return subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], check=True, text=True, capture_output=True).stdout
tools/translation.py:16:import tempfile
tools/process_lifecycle.py:1:"""Ownership-safe lifecycle for subprocess trees started by one BookText run."""
tools/process_lifecycle.py:7:import subprocess
tools/process_lifecycle.py:37:        self.process = subprocess.Popen(self.command, start_new_session=True, **kwargs)
tools/process_lifecycle.py:50:            completed = subprocess.run(
tools/process_lifecycle.py:147:        except subprocess.TimeoutExpired:
tools/project_history.py:22:_TRANSIENT_PREFIXES = ("/tmp/", "/private/tmp/", "/private/var/folders/")
tools/diagnostic_artifacts.py:3:import tempfile
tools/diagnostic_artifacts.py:12:    forbidden = [Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()]
tools/automatic_extraction.py:14:import tempfile
tools/automatic_extraction.py:27:    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
tools/export_document.py:13:import subprocess
tools/export_document.py:131:    subprocess.run([executable, str(markdown), "--resource-path", resources, "-o", str(destination)], check=True, capture_output=True, text=True)
tools/export_document.py:298:        check = subprocess.run([candidate, "-c", "import reportlab"], capture_output=True)
tools/export_document.py:304:    result = subprocess.run([_renderer_python(), str(Path(__file__).resolve()), "_render_pdf", str(document_path), str(destination)], capture_output=True, text=True)
tools/export_document.py:316:        result = subprocess.run(
tools/export_document.py:325:        result = subprocess.run(
tools/export_document.py:344:        if tool and subprocess.run([tool, str(path)], capture_output=True).returncode != 0: raise ValueError("unreadable PDF")
tools/hybrid_consensus_audit_v14.py:13:import subprocess
tools/hybrid_consensus_audit_v14.py:78:p = subprocess.run(
tools/review_decisions.py:12:import tempfile
tools/review_decisions.py:68:    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
```
