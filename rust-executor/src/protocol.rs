//! ExecutionRequest / ExecutionResult 的版本化协议（`schema_version = "1"`）。
//!
//! 协议只描述「要执行什么」和「执行结果是什么」，**不描述如何隔离**——
//! 隔离由 Docker 后端负责（见 `docker.rs`）。`schema_version` 保持不变，
//! V0.3 只是把原先固定拒绝的占位实现替换成真正的执行后端。

use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// 协议版本。字段形状变化时必须升版本，Python 侧据此拒绝过期请求。
pub const SCHEMA_VERSION: &str = "1";

/// 单次请求的读取上限，避免被超大输入撑爆内存。
pub const MAX_REQUEST_BYTES: u64 = 1024 * 1024;

/// 单条命令参数的长度上限。
const MAX_ARG_BYTES: usize = 16 * 1024;
/// 全部参数加起来的上限。
const MAX_ARGV_BYTES: usize = 64 * 1024;
/// 时间/资源限制的允许区间——请求可以要求更小，但不能要求更大。
const MIN_TIMEOUT_SECONDS: f64 = 0.1;
const MAX_TIMEOUT_SECONDS: f64 = 600.0;
const MIN_MEMORY_MB: u32 = 16;
const MAX_MEMORY_MB: u32 = 8192;
const MIN_CPUS: f64 = 0.1;
const MAX_CPUS: f64 = 8.0;
const MIN_PIDS: u32 = 1;
const MAX_PIDS: u32 = 4096;
const MIN_OUTPUT_BYTES: usize = 1024;
const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
const MAX_ARTIFACT_BYTES: u64 = 50 * 1024 * 1024;
const MAX_ARTIFACTS: usize = 100;

pub const DEFAULT_TIMEOUT_SECONDS: f64 = 30.0;
pub const DEFAULT_MEMORY_MB: u32 = 512;
pub const DEFAULT_CPUS: f64 = 1.0;
pub const DEFAULT_PIDS: u32 = 128;
pub const DEFAULT_OUTPUT_BYTES: usize = 64 * 1024;
pub const DEFAULT_ARTIFACT_BYTES: u64 = 5 * 1024 * 1024;
pub const DEFAULT_ARTIFACTS: usize = 20;

pub const STATUS_SUCCEEDED: &str = "succeeded";
pub const STATUS_FAILED: &str = "failed";
pub const STATUS_TIMEOUT: &str = "timeout";
pub const STATUS_REJECTED: &str = "rejected";

/// 协议或请求内容不合法。这类错误不会去碰 Docker。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError(pub String);

impl ProtocolError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// 客户端（Python）发来的执行请求。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionRequest {
    pub schema_version: String,
    /// 工具名，仅用于日志/排查，不参与执行。
    #[serde(default)]
    pub tool: String,
    /// 容器镜像，必须是合法引用且不能以 `-` 开头（否则会被 docker 当成选项）。
    pub image: String,
    /// 容器内要执行的命令，逐项传递，不经过宿主机 shell。
    pub argv: Vec<String>,
    /// 宿主机上的工作目录，会被挂载到容器 `/workspace`。
    pub workspace_dir: String,
    #[serde(default)]
    pub timeout_seconds: Option<f64>,
    #[serde(default)]
    pub memory_mb: Option<u32>,
    #[serde(default)]
    pub cpus: Option<f64>,
    #[serde(default)]
    pub pids_limit: Option<u32>,
    #[serde(default)]
    pub max_output_bytes: Option<usize>,
    /// 0 表示不限制单个产物文件大小。
    #[serde(default)]
    pub max_artifact_bytes: Option<u64>,
    #[serde(default)]
    pub max_artifacts: Option<usize>,
    /// 容器内运行的用户（`uid:gid`）。缺省按平台选择，见 `default_user`。
    #[serde(default)]
    pub user: Option<String>,
}

/// 通过校验、可以直接交给 Docker 后端的执行计划。
#[derive(Debug, Clone, PartialEq)]
pub struct Job {
    pub tool: String,
    pub image: String,
    pub argv: Vec<String>,
    pub workspace: PathBuf,
    pub timeout: Duration,
    pub memory_mb: u32,
    pub cpus: f64,
    pub pids_limit: u32,
    pub max_output_bytes: usize,
    pub max_artifact_bytes: u64,
    pub max_artifacts: usize,
    pub user: String,
}

impl ExecutionRequest {
    /// 校验并收敛成一个 Job。任何越界都会显式报错或被夹到允许区间。
    pub fn into_job(self) -> Result<Job, ProtocolError> {
        if self.schema_version != SCHEMA_VERSION {
            return Err(ProtocolError::new(format!(
                "期望 schema_version \"{SCHEMA_VERSION}\"，实际收到 \"{}\"",
                self.schema_version
            )));
        }

        check_image(&self.image)?;
        check_argv(&self.argv)?;

        let workspace = PathBuf::from(&self.workspace_dir);
        if !workspace.is_absolute() {
            return Err(ProtocolError::new("workspace_dir 必须是绝对路径"));
        }
        match std::fs::metadata(&workspace) {
            Ok(meta) if meta.is_dir() => {}
            Ok(_) => return Err(ProtocolError::new("workspace_dir 不是目录")),
            Err(err) => {
                return Err(ProtocolError::new(format!(
                    "workspace_dir 不可用: {err}"
                )))
            }
        }

        let user = match self.user {
            Some(user) => {
                check_user(&user)?;
                user
            }
            None => default_user(),
        };

        Ok(Job {
            tool: self.tool,
            image: self.image,
            argv: self.argv,
            workspace,
            timeout: Duration::from_secs_f64(clamp_f64(
                self.timeout_seconds,
                DEFAULT_TIMEOUT_SECONDS,
                MIN_TIMEOUT_SECONDS,
                MAX_TIMEOUT_SECONDS,
            )),
            memory_mb: self
                .memory_mb
                .unwrap_or(DEFAULT_MEMORY_MB)
                .clamp(MIN_MEMORY_MB, MAX_MEMORY_MB),
            cpus: clamp_f64(self.cpus, DEFAULT_CPUS, MIN_CPUS, MAX_CPUS),
            pids_limit: self
                .pids_limit
                .unwrap_or(DEFAULT_PIDS)
                .clamp(MIN_PIDS, MAX_PIDS),
            max_output_bytes: self
                .max_output_bytes
                .unwrap_or(DEFAULT_OUTPUT_BYTES)
                .clamp(MIN_OUTPUT_BYTES, MAX_OUTPUT_BYTES),
            max_artifact_bytes: self
                .max_artifact_bytes
                .unwrap_or(DEFAULT_ARTIFACT_BYTES)
                .min(MAX_ARTIFACT_BYTES),
            max_artifacts: self.max_artifacts.unwrap_or(DEFAULT_ARTIFACTS).min(MAX_ARTIFACTS),
            user,
        })
    }
}

fn clamp_f64(value: Option<f64>, default: f64, min: f64, max: f64) -> f64 {
    match value {
        Some(v) if v.is_finite() => v.clamp(min, max),
        _ => default,
    }
}

fn check_image(image: &str) -> Result<(), ProtocolError> {
    if image.is_empty() || image.len() > 200 {
        return Err(ProtocolError::new("image 为空或过长"));
    }
    // 不能以 '-' 开头：否则 docker 会把它当作命令行选项。
    if !image
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_alphanumeric())
    {
        return Err(ProtocolError::new(
            "image 必须以字母或数字开头（防止被当作 docker 选项）",
        ));
    }
    if !image.chars().all(|c| {
        c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | '/' | ':' | '@')
    }) {
        return Err(ProtocolError::new(
            "image 含有不支持的字符（仅允许字母、数字和 . _ - / : @）",
        ));
    }
    Ok(())
}

fn check_argv(argv: &[String]) -> Result<(), ProtocolError> {
    if argv.is_empty() {
        return Err(ProtocolError::new("argv 不能为空"));
    }
    let mut total = 0usize;
    for (index, arg) in argv.iter().enumerate() {
        if arg.is_empty() {
            return Err(ProtocolError::new(format!("argv[{index}] 为空字符串")));
        }
        if arg.contains('\0') {
            return Err(ProtocolError::new(format!("argv[{index}] 含有 NUL 字节")));
        }
        if arg.len() > MAX_ARG_BYTES {
            return Err(ProtocolError::new(format!("argv[{index}] 超过长度上限")));
        }
        total += arg.len();
    }
    if total > MAX_ARGV_BYTES {
        return Err(ProtocolError::new("argv 总长度超过上限"));
    }
    Ok(())
}

fn check_user(user: &str) -> Result<(), ProtocolError> {
    let (uid, gid) = user
        .split_once(':')
        .ok_or_else(|| ProtocolError::new("user 必须是 uid:gid 形式"))?;
    let digits = |part: &str| !part.is_empty() && part.len() <= 7 && part.chars().all(|c| c.is_ascii_digit());
    if !digits(uid) || !digits(gid) || user.matches(':').count() != 1 {
        return Err(ProtocolError::new("user 必须是 uid:gid 形式的十进制数字"));
    }
    Ok(())
}

/// 默认容器用户。
///
/// - Linux/macOS：`65534:65534`（nobody），容器内非 root；
/// - Windows：Docker Desktop 的绑定挂载对 uid 映射不可靠，默认用容器内 root
///   （配合 `--read-only`、`--cap-drop ALL`、`--network none` 仍然是有边界的），
///   运维可以通过请求里的 `user` 或 Python 侧配置覆盖。
pub fn default_user() -> String {
    if cfg!(windows) {
        "0:0".to_string()
    } else {
        "65534:65534".to_string()
    }
}

/// 产物清单条目。`size` 是宿主机侧重新读取的字节数，Python 会再算一次 SHA-256。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Artifact {
    pub path: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExecError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionResult {
    pub schema_version: String,
    /// succeeded | failed | timeout | rejected
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
    pub duration_ms: u64,
    pub artifacts: Vec<Artifact>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ExecError>,
}

impl ExecutionResult {
    pub fn rejected(code: &str, message: impl Into<String>) -> Self {
        Self {
            schema_version: SCHEMA_VERSION.to_string(),
            status: STATUS_REJECTED.to_string(),
            exit_code: None,
            stdout: String::new(),
            stderr: String::new(),
            stdout_truncated: false,
            stderr_truncated: false,
            duration_ms: 0,
            artifacts: Vec::new(),
            error: Some(ExecError {
                code: code.to_string(),
                message: message.into(),
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn workspace() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "agent-executor-protocol-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn request(workspace: &PathBuf) -> ExecutionRequest {
        ExecutionRequest {
            schema_version: SCHEMA_VERSION.to_string(),
            tool: "shell".to_string(),
            image: "python:3.12-slim".to_string(),
            argv: vec!["sh".into(), "-lc".into(), "echo hi".into()],
            workspace_dir: workspace.to_string_lossy().into_owned(),
            timeout_seconds: Some(5.0),
            memory_mb: Some(256),
            cpus: Some(1.0),
            pids_limit: Some(64),
            max_output_bytes: Some(8192),
            max_artifact_bytes: Some(1024),
            max_artifacts: Some(5),
            user: None,
        }
    }

    #[test]
    fn valid_request_becomes_job() {
        let dir = workspace();
        let job = request(&dir).into_job().unwrap();
        assert_eq!(job.argv[2], "echo hi");
        assert_eq!(job.timeout, Duration::from_secs_f64(5.0));
        assert_eq!(job.memory_mb, 256);
        assert_eq!(job.max_artifacts, 5);
        assert!(!job.user.is_empty());
    }

    #[test]
    fn rejects_unsupported_schema_version() {
        let dir = workspace();
        let mut req = request(&dir);
        req.schema_version = "2".to_string();
        assert!(req.into_job().unwrap_err().0.contains("schema_version"));
    }

    #[test]
    fn rejects_option_injection_in_image() {
        let dir = workspace();
        let mut req = request(&dir);
        req.image = "--volume=/:/host".to_string();
        assert!(req.into_job().is_err());
    }

    #[test]
    fn rejects_relative_workspace() {
        let dir = workspace();
        let mut req = request(&dir);
        req.workspace_dir = "relative/dir".to_string();
        assert!(req.into_job().is_err());
    }

    #[test]
    fn rejects_missing_workspace() {
        let dir = workspace();
        let mut req = request(&dir);
        req.workspace_dir = dir.join("missing").to_string_lossy().into_owned();
        assert!(req.into_job().is_err());
    }

    #[test]
    fn rejects_empty_argv() {
        let dir = workspace();
        let mut req = request(&dir);
        req.argv.clear();
        assert!(req.into_job().is_err());
    }

    #[test]
    fn rejects_bad_user() {
        let dir = workspace();
        let mut req = request(&dir);
        req.user = Some("root".to_string());
        assert!(req.into_job().is_err());
    }

    #[test]
    fn clamps_out_of_range_limits() {
        let dir = workspace();
        let mut req = request(&dir);
        req.timeout_seconds = Some(100_000.0);
        req.memory_mb = Some(1);
        req.cpus = Some(999.0);
        req.pids_limit = Some(0);
        req.max_output_bytes = Some(10);
        let job = req.into_job().unwrap();
        assert_eq!(job.timeout, Duration::from_secs_f64(MAX_TIMEOUT_SECONDS));
        assert_eq!(job.memory_mb, MIN_MEMORY_MB);
        assert_eq!(job.cpus, MAX_CPUS);
        assert_eq!(job.pids_limit, MIN_PIDS);
        assert_eq!(job.max_output_bytes, MIN_OUTPUT_BYTES);
    }
}
