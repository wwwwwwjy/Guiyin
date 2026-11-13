# Third-party source notices

本组合发行及新增 Hub 代码采用 GPL-3.0-or-later，全文见根目录 LICENSE。以下原有文件继续保留各自版权与许可：

- `app/` 中除 `lx_runtime.js`、`lx_worker.py`、`hub_*.py`、`static/studio.*`、`static/hub-icon.svg`、`netease_provider.py` 外的上游代码来自 [baimoushare/fnmusic-bridge](https://github.com/baimoushare/fnmusic-bridge)，MIT，原声明保留于 `licenses/FNMUSIC_BRIDGE_MIT`。上游快照见 `licenses/UPSTREAM_VERSIONS.json`。
- `app/netease_provider.py` 来自 [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url) 的 `music_api.py`，MIT，见 `licenses/NETEASE_MIT`。增加搜索结果时长字段。
- `qqmusic-api-python==0.9.0` 来自 [L-1124/QQMusicApi](https://github.com/L-1124/QQMusicApi)，GPL-3.0-or-later，见 `licenses/QQMUSIC_GPL`。未修改的对应 PyPI 源码分发包包含于 `third_party/qqmusic_api_python-0.9.0.tar.gz`，Docker 内保存在 `/app/third_party/`；构建按 requirements 安装同一发行版本。
- 酷狗网关签名、登录歌单、搜索、音源及榜单协议参考 [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)，MIT 声明保留于 `licenses/KUGOU_MIT`；本包使用 Python 适配，不运行该项目的 Node 服务。
- 未复制或使用参考项目中的第三方账号凭据。

对上游文件的修改：

- `webapp.py`：注册归音路由、旧入口重定向归音、认证检查、禁用上游镜像自动覆盖、增加 `hub_reconcile` 子进程派发及写锁分类。
- `bridge_store.py`：让新任务进入原有歌单资源互斥。
- `kugou.py` / `kugou_qr.py`：使用受信 CA 证书包，保留媒体标识，并支持榜单请求头。
- 原上游管理界面、收款码、未使用的上游镜像更新器已移除；归音界面及打赏入口由本项目实现；内置的两张收款码由归音项目作者提供，不属于上游项目。
- `fnsync.py`：共享归音完整来源快照；差量删除快照禁止走全量覆盖恢复。原生快照、API、匹配与评论算法沿用上游，归音新增关联所有权和差量同步。

新增的 `hub_*.py`、`static/studio.*`、`static/hub-icon.svg`、部署及测试文件版权归本项目贡献者所有，GPL-3.0-or-later。构建脚本、前后端源码及测试随源码包完整提供。其他 Python 依赖的许可随其安装包保留；可用 requirements.txt 重建环境。

本项目不是上述项目的官方发布。请使用本项目 Dockerfile 重建，勿用上游更新镜像覆盖扩展。

## 0.9.12 reference improvements

- [jia070310/lemon-muisc](https://github.com/jia070310/lemon-muisc), MIT, Copyright (c) 2026 jia070310: referenced `server/utils/audioFormat.js`, `downloadQuality.js` and `sourceFallback.js` for actual-codec checks and transfer-failure fallback. Independently adapted to Python in `hub_audio_validation.py` and `hub_engine.py`; not a bundled Lemon runtime or source plugin. Full license: `licenses/LEMON_MUSIC_MIT`.
- [baimoushare/fnmusic-bridge](https://github.com/baimoushare/fnmusic-bridge), MIT: `fnsync.py:do_import` reverse-batch insertion strategy adapted into managed delta additions, retaining source order among new tracks while preserving existing/manual relations.

## 0.9.14 JS source support

- QuickJS Python binding 1.19.4 (PetterS/quickjs), MIT; bundled QuickJS engine by Fabrice Bellard and Charlie Gordon, MIT. Installed package retains their licenses. `lx_runtime.js`, `lx_worker.py` and `hub_js_sources.py` are original Guiyin adapter code, GPL-3.0-or-later.
- User-imported JS files retain their original author/license. Importing into a private data volume does not add them to this source distribution. The project ships an empty custom-source registry; the supplied private source files/API keys are not redistributed.
