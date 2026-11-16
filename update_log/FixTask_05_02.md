# FixTask_05_02.md — `AtomicCalculationDB` (V2.4.1) 导出校对包后打开资源管理器

## 1. 问题

V2.4.0 导出 AI 校对包后，只弹出一个包含路径的提示框；用户不清楚文件实际导出到了哪里，需要手动复制路径查找。

## 2. 修改

- `DocumentDetailDialog._export_audit_bundle()` 在导出成功提示后调用：
  ```python
  QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.export_dir)))
  ```
- 自动用系统文件资源管理器打开本次导出目录。
- 不改变导出目录结构、报告内容或审计导入行为。

## 3. 验证

- 新增 GUI 测试： monkeypatch `QDesktopServices.openUrl`，确认导出后收到的本地路径存在且为目录。
- 全量回归：**132 passed**。

## 4. 构建与部署

- 版本：**V2.4.1**
- 发布包：
  - `release/AtomicCalculationDB_Patch_v2.4.1.zip`（438,683 B，87 个文件）
    - SHA256 `48cf56af1fe29eed502c0270a12f57ea2127f5c985c17958fa5818159b20d55d`
  - `release/AtomicCalculationDB_Full_v2.4.1.zip`（1,714,467,156 B，9,952 个文件）
    - SHA256 `a0ddf2810e1161063dfebaeb1e1eb9e19aab4546a9715545c0b84c3c09793a37`
- 两个包均通过 `unzip -t`。
- 安装目录仍为 V2.3.11，应用未运行；本次未覆盖安装。
