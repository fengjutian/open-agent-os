//! 产物收集。
//!
//! 约定：只有 `<workspace>/output/` 下的普通文件才算产物，其余文件（包括命令自己
//! 写在 workspace 根目录的中间文件）不会回传。这样「什么算交付物」是显式约定，
//! 而不是把整个工作目录打包回去。
//!
//! 不跟随符号链接：容器里可以创建指向 `/etc/passwd` 的软链，跟随就会把宿主机/
//! 容器内的敏感文件读出来。

use std::path::Path;

use crate::protocol::Artifact;

/// 目录递归深度上限，防止恶意构造的深层目录拖垮 walker。
const MAX_DEPTH: usize = 8;

/// 扫描 `root`，返回按路径排序的产物清单。
///
/// - `max_files == 0` 表示不收集；
/// - `max_bytes == 0` 表示不限制单文件大小；
/// - 超过大小上限的文件被跳过（不会出现在清单里）。
pub fn collect(root: &Path, max_files: usize, max_bytes: u64) -> Vec<Artifact> {
    if max_files == 0 || !root.is_dir() {
        return Vec::new();
    }
    let mut found = Vec::new();
    walk(root, root, 0, max_files, max_bytes, &mut found);
    found.sort_by(|a, b| a.path.cmp(&b.path));
    found.truncate(max_files);
    found
}

fn walk(
    root: &Path,
    dir: &Path,
    depth: usize,
    max_files: usize,
    max_bytes: u64,
    out: &mut Vec<Artifact>,
) {
    if depth > MAX_DEPTH || out.len() >= max_files {
        return;
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    let mut entries: Vec<_> = entries.flatten().collect();
    entries.sort_by_key(|entry| entry.file_name());

    for entry in entries {
        if out.len() >= max_files {
            return;
        }
        let path = entry.path();
        let meta = match std::fs::symlink_metadata(&path) {
            Ok(meta) => meta,
            Err(_) => continue,
        };
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            walk(root, &path, depth + 1, max_files, max_bytes, out);
            continue;
        }
        if !meta.is_file() {
            continue;
        }
        if max_bytes > 0 && meta.len() > max_bytes {
            continue;
        }
        let Ok(relative) = path.strip_prefix(root) else {
            continue;
        };
        let relative = relative.to_string_lossy().replace('\\', "/");
        if relative.is_empty() || relative.starts_with("..") {
            continue;
        }
        out.push(Artifact {
            path: relative,
            size: meta.len(),
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "agent-executor-artifacts-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn collects_files_recursively_and_sorts() {
        let root = temp_root();
        std::fs::create_dir_all(root.join("nested")).unwrap();
        std::fs::write(root.join("b.txt"), b"bb").unwrap();
        std::fs::write(root.join("a.txt"), b"a").unwrap();
        std::fs::write(root.join("nested/c.txt"), b"ccc").unwrap();

        let artifacts = collect(&root, 10, 0);
        let paths: Vec<_> = artifacts.iter().map(|a| a.path.as_str()).collect();
        assert_eq!(paths, vec!["a.txt", "b.txt", "nested/c.txt"]);
        assert_eq!(artifacts[2].size, 3);

        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn skips_oversized_files() {
        let root = temp_root();
        std::fs::write(root.join("small.txt"), b"ok").unwrap();
        std::fs::write(root.join("big.bin"), vec![0u8; 64]).unwrap();

        let artifacts = collect(&root, 10, 8);
        assert_eq!(artifacts.len(), 1);
        assert_eq!(artifacts[0].path, "small.txt");

        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn respects_file_limit_and_missing_dir() {
        let root = temp_root();
        for index in 0..5 {
            std::fs::write(root.join(format!("{index}.txt")), b"x").unwrap();
        }
        assert_eq!(collect(&root, 2, 0).len(), 2);
        assert!(collect(&root.join("missing"), 10, 0).is_empty());
        assert!(collect(&root, 0, 0).is_empty());

        std::fs::remove_dir_all(&root).ok();
    }
}
