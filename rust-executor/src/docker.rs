//! Docker 执行后端。
//!
//! 隔离全部由 Docker 提供，Rust 进程只负责拼参数、看超时、收输出。
//! **不要**把这里换成 `std::process::Command` 直接跑用户命令——那没有安全边界。
//!
//! 关键参数（缺一不可）：
//!
//! - `--network none`：默认禁网；
//! - `--read-only` + `--tmpfs /tmp`：根文件系统只读，只留一个可写的临时区；
//! - `--cap-drop ALL` + `--security-opt no-new-privileges`：去掉特权；
//! - `--pids-limit` / `--memory` / `--cpus`：资源上限；
//! - `--user`：默认非 root（Windows 上 Docker Desktop 的 uid 映射不可靠，见
//!   `protocol::default_user`）；
//! - 只挂载本次调用的 workspace 到 `/workspace`，不挂 docker socket、不挂宿主机目录。

use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::process::{ExitStatus, Stdio};
use std::time::{Duration, Instant};

use tokio::process::Command;
use tokio::time::timeout;

use crate::artifacts;
use crate::protocol::{
    Artifact, ExecError, ExecutionResult, Job, SCHEMA_VERSION, STATUS_FAILED, STATUS_REJECTED,
    STATUS_SUCCEEDED, STATUS_TIMEOUT,
};

const DOCKER_BIN_ENV: &str = "AGENT_EXECUTOR_DOCKER_BIN";
/// 超时后等 `docker kill` 收尾的上限。
const KILL_TIMEOUT: Duration = Duration::from_secs(10);

/// 本次执行的临时容器名。`--rm` 会在正常退出时清理，超时路径用它兜底。
pub fn container_name() -> String {
    format!("agent-exec-{}", uuid::Uuid::new_v4().simple())
}

/// 读取 `AGENT_EXECUTOR_DOCKER_BIN`，允许带参数（例如 `python fake_docker.py`、
/// `sudo -n docker`、podman 兼容包装脚本）。默认是 `docker`。
pub fn docker_command() -> Vec<String> {
    let raw = std::env::var(DOCKER_BIN_ENV).unwrap_or_else(|_| "docker".to_string());
    split_command(&raw)
}

/// 极简命令拆分：支持单/双引号，够覆盖路径里带空格的场景。
fn split_command(raw: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let mut quote: Option<char> = None;
    let mut has_token = false;

    for ch in raw.chars() {
        match quote {
            Some(expected) if ch == expected => quote = None,
            Some(_) => {
                current.push(ch);
                has_token = true;
            }
            None if ch == '"' || ch == '\'' => {
                quote = Some(ch);
                has_token = true;
            }
            None if ch.is_whitespace() => {
                if has_token {
                    parts.push(std::mem::take(&mut current));
                    has_token = false;
                }
            }
            None => {
                current.push(ch);
                has_token = true;
            }
        }
    }
    if has_token {
        parts.push(current);
    }
    if parts.is_empty() {
        parts.push("docker".to_string());
    }
    parts
}

/// 把宿主机路径转成 Docker 能接受的挂载源。
///
/// Windows 上 `/` 和 `\` 都能被 Docker Desktop 识别，但统一用 `/` 可以避免
/// `-v C:\x:/workspace` 里的冒号歧义；`\\?\` 前缀是 Rust canonicalize 的产物，
/// Docker 不认识，需要去掉。
pub fn mount_path(path: &Path) -> String {
    let raw = path.to_string_lossy();
    let raw = raw.strip_prefix(r"\\?\").unwrap_or(&raw);
    raw.replace('\\', "/")
}

/// 拼出 `docker run ...` 的参数（不含 docker 自身）。
///
/// 单独暴露出来是为了能在没有 Docker 的环境里用单元测试锁住隔离参数。
pub fn build_args(job: &Job, name: &str) -> Vec<String> {
    let memory = format!("{}m", job.memory_mb);
    let mut args: Vec<String> = vec![
        "run".into(),
        "--rm".into(),
        "--init".into(),
        "--name".into(),
        name.to_string(),
        "--network".into(),
        "none".into(),
        "--read-only".into(),
        "--tmpfs".into(),
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777".into(),
        "--cap-drop".into(),
        "ALL".into(),
        "--security-opt".into(),
        "no-new-privileges".into(),
        "--pids-limit".into(),
        job.pids_limit.to_string(),
        "--memory".into(),
        memory.clone(),
        "--memory-swap".into(),
        memory,
        "--cpus".into(),
        format!("{}", job.cpus),
        "--user".into(),
        job.user.clone(),
        "-e".into(),
        "HOME=/tmp".into(),
        "-e".into(),
        "PYTHONDONTWRITEBYTECODE=1".into(),
        "--label".into(),
        "agent-executor=1".into(),
        "-v".into(),
        format!("{}:/workspace:rw", mount_path(&job.workspace)),
    ];
    // 输入文件以**只读**方式挂到 /workspace 下。放在 workspace 挂载之后，
    // 更具体的路径才会覆盖同名路径（Docker 按出现顺序处理挂载）。
    for item in &job.inputs {
        args.push("-v".into());
        args.push(format!(
            "{}:/workspace/{}:ro",
            mount_path(Path::new(&item.source)),
            item.target
        ));
    }
    args.extend([
        "-w".to_string(),
        "/workspace".to_string(),
        job.image.clone(),
    ]);
    args.extend(job.argv.iter().cloned());
    args
}

enum Outcome {
    Succeeded(Option<i32>),
    Failed(Option<i32>),
    TimedOut,
    Rejected(&'static str, String),
}

/// 执行一次任务并组装完整结果。
pub async fn run(job: &Job, name: &str) -> ExecutionResult {
    let started = Instant::now();
    let log_dir = std::env::temp_dir().join(format!("agent-executor-logs-{name}"));
    if let Err(err) = std::fs::create_dir_all(&log_dir) {
        return ExecutionResult::rejected("executor_error", format!("无法创建日志目录: {err}"));
    }
    let stdout_path = log_dir.join("stdout.log");
    let stderr_path = log_dir.join("stderr.log");

    let outcome = execute(job, name, &stdout_path, &stderr_path).await;

    let (stdout, stdout_truncated) = read_capped(&stdout_path, job.max_output_bytes);
    let (stderr, stderr_truncated) = read_capped(&stderr_path, job.max_output_bytes);

    let (status, exit_code, error) = match outcome {
        Outcome::Succeeded(code) => (STATUS_SUCCEEDED, code, None),
        Outcome::Failed(code) => (STATUS_FAILED, code, None),
        Outcome::TimedOut => (STATUS_TIMEOUT, None, None),
        Outcome::Rejected(code, message) => {
            let message = if message.is_empty() {
                summarize(&stderr)
            } else {
                message
            };
            (
                STATUS_REJECTED,
                None,
                Some(ExecError {
                    code: code.to_string(),
                    message,
                }),
            )
        }
    };

    let artifacts: Vec<Artifact> = artifacts::collect(
        &job.workspace.join("output"),
        job.max_artifacts,
        job.max_artifact_bytes,
    );

    let result = ExecutionResult {
        schema_version: SCHEMA_VERSION.to_string(),
        status: status.to_string(),
        exit_code,
        stdout,
        stderr,
        stdout_truncated,
        stderr_truncated,
        duration_ms: started.elapsed().as_millis() as u64,
        artifacts,
        error,
    };

    let _ = std::fs::remove_dir_all(&log_dir);
    result
}

async fn execute(
    job: &Job,
    name: &str,
    stdout_path: &Path,
    stderr_path: &Path,
) -> Outcome {
    let stdout = match File::create(stdout_path) {
        Ok(file) => file,
        Err(err) => {
            return Outcome::Rejected("executor_error", format!("无法创建 stdout 日志: {err}"))
        }
    };
    let stderr = match File::create(stderr_path) {
        Ok(file) => file,
        Err(err) => {
            return Outcome::Rejected("executor_error", format!("无法创建 stderr 日志: {err}"))
        }
    };

    let docker = docker_command();
    let display = docker.join(" ");
    let mut child = match Command::new(&docker[0])
        .args(&docker[1..])
        .args(build_args(job, name))
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
    {
        Ok(child) => child,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Outcome::Rejected(
                "executor_unavailable",
                format!("找不到 docker 可执行文件（{display}）"),
            )
        }
        Err(err) => {
            return Outcome::Rejected("executor_error", format!("启动 docker 失败: {err}"))
        }
    };

    match timeout(job.timeout, child.wait()).await {
        Ok(Ok(status)) => classify(status),
        Ok(Err(err)) => {
            Outcome::Rejected("executor_error", format!("等待 docker 退出失败: {err}"))
        }
        Err(_) => {
            kill_container(&docker, name).await;
            let _ = child.kill().await;
            let _ = timeout(Duration::from_secs(5), child.wait()).await;
            Outcome::TimedOut
        }
    }
}

fn classify(status: ExitStatus) -> Outcome {
    // 125 是 Docker CLI 自身的错误（镜像不存在、daemon 没起来等），
    // 和「容器里的命令失败」是两回事，必须分开报。
    if status.code() == Some(125) {
        return Outcome::Rejected("executor_unavailable", String::new());
    }
    if status.success() {
        Outcome::Succeeded(status.code())
    } else {
        Outcome::Failed(status.code())
    }
}

async fn kill_container(docker: &[String], name: &str) {
    let _ = timeout(
        KILL_TIMEOUT,
        Command::new(&docker[0])
            .args(&docker[1..])
            .args(["kill", name])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status(),
    )
    .await;
    // `--rm` 通常已经清掉了，这里兜底处理 kill 失败（容器已退出等）的情况。
    let _ = timeout(
        KILL_TIMEOUT,
        Command::new(&docker[0])
            .args(&docker[1..])
            .args(["rm", "-f", name])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status(),
    )
    .await;
}

/// 读日志文件的前 `max` 字节，并标记是否被截断。
fn read_capped(path: &Path, max: usize) -> (String, bool) {
    let mut buffer = Vec::new();
    if let Ok(file) = File::open(path) {
        // 多读 1 字节用来判断是否截断；内存占用被 max 限制住。
        let _ = file.take(max as u64 + 1).read_to_end(&mut buffer);
    }
    let truncated = buffer.len() > max;
    buffer.truncate(max);
    (String::from_utf8_lossy(&buffer).into_owned(), truncated)
}

fn summarize(text: &str) -> String {
    let compact = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if compact.is_empty() {
        return "docker 拒绝了执行请求（无 stderr 输出）".to_string();
    }
    let mut chars = compact.chars();
    let head: String = chars.by_ref().take(300).collect();
    if chars.next().is_some() {
        format!("{head}…")
    } else {
        head
    }
}

/// 供运维判断本机是否具备执行条件（`agent-executor --check`）。
pub fn docker_available() -> bool {
    let docker = docker_command();
    std::process::Command::new(&docker[0])
        .args(&docker[1..])
        .args(["version", "--format", "{{.Server.Version}}"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{InputMount, Job};
    use std::path::PathBuf;
    use std::time::Duration;

    fn job() -> Job {
        Job {
            tool: "shell".to_string(),
            image: "python:3.12-slim".to_string(),
            argv: vec!["sh".into(), "-lc".into(), "echo hi".into()],
            workspace: PathBuf::from("C:/tmp/agent-workspace"),
            timeout: Duration::from_secs(10),
            memory_mb: 512,
            cpus: 1.0,
            pids_limit: 128,
            max_output_bytes: 65536,
            max_artifact_bytes: 5 * 1024 * 1024,
            max_artifacts: 20,
            user: "65534:65534".to_string(),
            inputs: Vec::new(),
        }
    }

    #[test]
    fn build_args_contains_isolation_flags() {
        let args = build_args(&job(), "agent-exec-test");
        let joined = args.join(" ");
        for flag in [
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--pids-limit 128",
            "--memory 512m",
            "--memory-swap 512m",
            "--cpus 1",
            "--user 65534:65534",
            "-w /workspace",
        ] {
            assert!(joined.contains(flag), "missing {flag}: {joined}");
        }
        assert!(!joined.contains("docker.sock"));
    }

    #[test]
    fn build_args_mounts_only_workspace_and_keeps_argv_order() {
        let args = build_args(&job(), "agent-exec-test");
        let volume = args
            .iter()
            .position(|arg| arg == "-v")
            .expect("missing -v");
        assert_eq!(args[volume + 1], "C:/tmp/agent-workspace:/workspace:rw");
        // image 之后紧跟 argv，Docker 不会再把它们当选项解析
        let image = args
            .iter()
            .position(|arg| arg == "python:3.12-slim")
            .expect("missing image");
        assert_eq!(&args[image + 1..], &["sh", "-lc", "echo hi"]);
    }

    #[test]
    fn build_args_mounts_inputs_read_only_under_workspace() {
        let mut job = job();
        job.inputs = vec![InputMount {
            source: r"C:\data\report.csv".to_string(),
            target: "input/report.csv".to_string(),
        }];
        let args = build_args(&job, "agent-exec-test");
        let joined = args.join(" ");

        // 输入文件必须只读挂载，且落在 /workspace 下
        assert!(
            joined.contains("C:/data/report.csv:/workspace/input/report.csv:ro"),
            "input mount missing: {joined}"
        );
        // 不能出现可写挂载
        assert!(!joined.contains(":/workspace/input/report.csv:rw"));
        // workspace 仍是可写的（产物要写进去）
        assert!(joined.contains("C:/tmp/agent-workspace:/workspace:rw"));
        // 只挂这一个输入，不该出现宿主机目录
        assert!(!joined.contains("docker.sock"));
    }

    #[test]
    fn mount_path_normalizes_windows_paths() {
        assert_eq!(
            mount_path(Path::new(r"C:\Users\dev\workspace")),
            "C:/Users/dev/workspace"
        );
        assert_eq!(
            mount_path(Path::new(r"\\?\C:\Users\dev\workspace")),
            "C:/Users/dev/workspace"
        );
    }

    #[test]
    fn summarize_is_bounded_and_single_line() {
        let text = format!("  line one\n\nline two {}", "x".repeat(1000));
        let summary = summarize(&text);
        assert!(summary.starts_with("line one line two"));
        assert!(summary.chars().count() <= 301);
    }

    #[test]
    fn split_command_supports_quoted_paths() {
        assert_eq!(split_command("docker"), vec!["docker"]);
        assert_eq!(
            split_command(r#""C:/Program Files/Python/python.exe" fake_docker.py"#),
            vec!["C:/Program Files/Python/python.exe", "fake_docker.py"]
        );
        assert_eq!(split_command("  sudo   -n docker "), vec!["sudo", "-n", "docker"]);
        assert_eq!(split_command("   "), vec!["docker"]);
    }

    #[test]
    fn read_capped_marks_truncation() {
        let path = std::env::temp_dir().join(format!(
            "agent-executor-capped-{}.log",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::write(&path, b"0123456789").unwrap();
        let (text, truncated) = read_capped(&path, 4);
        assert_eq!(text, "0123");
        assert!(truncated);
        std::fs::remove_file(&path).ok();
    }
}
