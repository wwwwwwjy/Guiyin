# 参与归音开发

欢迎通过 Issue 提交可复现的问题，通过 Pull Request 改进功能、文档或适配。

## 本地运行

使用 Python 3.12，创建虚拟环境并安装 `requirements.txt`。可用 `compose.local.yaml` 在独立数据目录验证界面，不要连接自己的生产数据做破坏性测试。飞牛相关功能需要真实 fnOS、飞牛音乐和对应挂载。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*unittest.py'
node --check app/static/studio.js
```

界面回归脚本：

```bash
node tests/test_source_import.js
node tests/test_source_forms.js
node tests/test_source_priority.js
node tests/test_job_selection.js
node tests/test_comment_directories.js
node tests/test_asset_directories.js
node tests/test_metadata_ui.js
node tests/test_support_ui.js
```

## 设计约定

- 来源列表不完整时保留上次完整快照，不能误当作歌曲已删除。
- 下载使用临时文件、格式/时长验证及原子发布，任务重试不能造成重复写入。
- 标签修复保守处理冲突，必须保留可验证的原文件备份。
- 网络请求遵守超时、限流与退避，错误信息不暴露认证凭据。
- 引入第三方代码时核实许可并更新 `THIRD_PARTY_NOTICES.md`。

## 发布文件

```bash
python tools/package_release.py --audit-only
python tools/package_release.py
```

脚本按清单打包，并校验内置作者图片与文档截图指纹。更新截图后同步 `docs/screenshots/manifest.json`。运行数据、`.env`、本地验证记录与私人源文件不属于公开发行内容。

提交前说明问题、最终行为和验证方式。涉及账号或数据安全的问题，请先通过 GitHub 私密漏洞报告（若仓库启用）或联系维护者处理，勿在公开 Issue 张贴凭据。
