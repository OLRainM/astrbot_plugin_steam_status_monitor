# Steam 状态监控插件升级与新版使用指南

本文档适用于从修改前的旧版本 `9178f3c`（V3.3.3）升级到当前 Fork 版本 `4f22bc5`，并介绍新版的安装、配置、使用、验证与回滚方法。

## 目录

- [1. 升级范围](#1-升级范围)
- [2. 新版主要变化](#2-新版主要变化)
- [3. 升级前准备](#3-升级前准备)
- [4. 备份旧版本](#4-备份旧版本)
- [5. 获取并部署新版](#5-获取并部署新版)
  - [5.1 使用 Git 更新](#51-使用-git-更新)
  - [5.2 使用压缩包更新](#52-使用压缩包更新)
  - [5.3 通过 AstrBot 安装](#53-通过-astrbot-安装)
- [6. 安装依赖](#6-安装依赖)
- [7. 恢复配置与数据](#7-恢复配置与数据)
- [8. 数据兼容与自动迁移](#8-数据兼容与自动迁移)
- [9. 启动与升级验证](#9-启动与升级验证)
- [10. 新版快速开始](#10-新版快速开始)
- [11. 新版指令说明](#11-新版指令说明)
  - [11.1 玩家与绑定](#111-玩家与绑定)
  - [11.2 状态、排行与成就](#112-状态排行与成就)
  - [11.3 每日排行推送](#113-每日排行推送)
  - [11.4 联动推送](#114-联动推送)
  - [11.5 配置与维护](#115-配置与维护)
- [12. 管理页面使用](#12-管理页面使用)
- [13. 主要配置项](#13-主要配置项)
- [14. 数据文件与保留周期](#14-数据文件与保留周期)
- [15. 常见问题排查](#15-常见问题排查)
- [16. 回滚方法](#16-回滚方法)
- [17. 升级检查清单](#17-升级检查清单)

## 1. 升级范围

| 项目 | 版本或要求 |
| --- | --- |
| 修改前旧版本 | `9178f3c`（V3.3.3） |
| 当前目标版本 | `4f22bc5` |
| Fork 仓库 | `https://github.com/OLRainM/astrbot_plugin_steam_status_monitor` |
| 默认分支 | `main` |
| 最低 AstrBot 版本 | `4.24.2` |

仓库中的 `metadata.yaml` 仍沿用上游版本号 `3.3.3`，因此升级时应以 Git 提交 SHA 判断是否已经部署当前 Fork 版本，而不能只看插件页面显示的版本号。

从旧版到目标版本包含以下提交：

```text
f77a47b 重构插件模块并完善项目文档
257ad66 优化管理接口高负载查询性能
3749de7 补充功能地图并保护本地数据
db92e68 功能(管理页): 重设计玩家数据统计界面
4f22bc5 修复(管理页): 写操作后同步刷新统计缓存
```

## 2. 新版主要变化

新版保持 AstrBot 插件入口和原有数据模式，重点完成了以下改造：

- 将旧版集中在入口文件中的业务逻辑拆分到 `src/` 分层模块；
- 保留根目录 `main.py` 作为 AstrBot 兼容入口；
- 新增和重构管理页面，提供仪表盘、群组、玩家、绑定、排行、热力图、通知及诊断功能；
- 为管理接口增加 TTL 缓存、同键并发合并和写操作后的缓存失效；
- 统一插件资源、页面、字体和数据路径；
- 增加旧 `start_play_times` 数据格式的兼容迁移；
- 增加模块结构、排行范围、管理页缓存和统计结果测试；
- 将本地运行数据目录加入 `.gitignore`，降低私有数据被误提交的风险。

新版仍通过 AstrBot 的 `context.register_web_api()` 注册管理接口，不会额外启动独立 Web 服务。

## 3. 升级前准备

升级前确认以下条件：

1. AstrBot 版本不低于 `4.24.2`；
2. 能够找到当前插件代码目录；
3. 能够找到 AstrBot 实际使用的插件配置和数据目录；
4. 已记录当前可用的 Steam Web API Key、代理设置和通知配置；
5. 有足够空间保存代码、配置和数据的完整备份；
6. 可以在升级期间停止 AstrBot，避免新旧代码同时写入数据文件。

> 升级过程中不要让新旧版本同时使用同一个 `data/steam_status_monitor/` 目录，否则可能出现状态覆盖、重复通知或 JSON 写入冲突。

## 4. 备份旧版本

停止 AstrBot 后，至少备份以下内容：

- 旧版插件代码目录；
- AstrBot 中该插件的真实配置；
- `data/steam_status_monitor/` 整个目录；
- 如使用容器，备份相关挂载目录和部署配置。

PowerShell 示例：

```powershell
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item "插件目录" "插件目录-backup-$timestamp" -Recurse
Copy-Item "data/steam_status_monitor" "steam_status_monitor-data-backup-$timestamp" -Recurse
```

备份后应确认以下关键文件存在：

```text
steam_groups.json
bind_data.json
notify_sessions.json
push_groups.json
rank_push_groups.json
play_records.json
session_records.json
```

实际目录可能只包含其中一部分，取决于旧版已使用的功能。

## 5. 获取并部署新版

### 5.1 使用 Git 更新

如果现有插件目录是 Git 仓库，并且没有需要保留的本地源码修改，可先确认状态：

```powershell
git status --short
git remote -v
```

推荐从 Fork 仓库获取目标分支：

```powershell
git remote add fork https://github.com/OLRainM/astrbot_plugin_steam_status_monitor.git
git fetch fork
git switch main
git merge --ff-only fork/main
```

如果已经存在名为 `fork` 的远端，不要重复添加，直接执行：

```powershell
git fetch fork
git switch main
git merge --ff-only fork/main
```

更新后检查版本：

```powershell
git rev-parse --short HEAD
```

目标输出为：

```text
4f22bc5
```

如果旧目录包含未提交修改，不要直接覆盖或强制重置。先备份并导出差异，再在新版上人工合并。

### 5.2 使用压缩包更新

如果旧版不是 Git 安装：

1. 从 Fork 仓库的 `main` 分支下载 ZIP；
2. 解压到新的临时目录；
3. 保留旧版代码和数据备份；
4. 用新版完整替换插件代码；
5. 恢复真实配置和数据目录；
6. 重启或重新加载插件。

必须完整部署以下内容：

```text
main.py
src/
assets/
pages/
_conf_schema.json
metadata.yaml
requirements.txt
```

不要只替换 `main.py`。新版实际业务实现位于 `src/`，管理页面资源位于 `pages/`，图片和字体资源位于 `assets/`。

### 5.3 通过 AstrBot 安装

可在 AstrBot 插件管理页面使用以下仓库地址安装：

```text
https://github.com/OLRainM/astrbot_plugin_steam_status_monitor
```

如果 AstrBot 将其识别为一个新的插件目录，应先停止旧插件，确认新插件使用的数据路径，再恢复旧数据。不要同时启用两个 Steam 状态监控实例。

## 6. 安装依赖

在 AstrBot 实际使用的 Python 环境中执行：

```powershell
python -m pip install -r requirements.txt
```

主要依赖包括：

- `httpx[socks]`：Steam API 请求及 SOCKS 代理支持；
- `aiohttp`：异步资源下载；
- `Pillow`：图片生成与处理；
- `requests`：同步图片下载；
- `numpy`：图片边界检测。

如果 AstrBot 运行在虚拟环境、Docker 或其他隔离环境中，必须在对应环境内安装，不能只安装到系统 Python。

## 7. 恢复配置与数据

升级后应优先保留 AstrBot 中已有的生产配置，不要用仓库中的示例值覆盖真实配置。

至少检查：

- `steam_api_key`；
- `sgdb_api_key`；
- `enable_proxy` 和 `proxy_url`；
- 轮询间隔与重试次数；
- 开始、结束、成就和网络波动通知开关；
- 图片与文本通知开关；
- 每日排行推送时间；
- 游戏过滤模式和 AppID 列表。

恢复数据时，应将备份的 `data/steam_status_monitor/` 放回 AstrBot 为该插件提供的实际数据目录。不要把数据文件放进源码目录，也不要将 API Key 或数据目录提交到 Git。

## 8. 数据兼容与自动迁移

新版继续使用 JSON 文件持久化群组、绑定、通知、状态、游玩记录和会话记录。

`src/infrastructure/persistence/plugin_data.py` 包含旧 `start_play_times` 格式的兼容逻辑。旧版中的整数时间值会在加载时迁移为新版结构；无法保留明确游戏维度的旧整数值会迁移为空字典，避免新版按错误结构继续处理。

升级后重点检查：

- 原群组是否仍能通过 `/steam list` 查询；
- 用户绑定和备注名是否存在；
- 通知会话是否可以正常发送；
- 排行数据和最近会话是否可读取；
- 日志中是否出现 JSON 解析或迁移异常。

如果旧版只使用配置项 `steam_ids`，但没有生成 `steam_groups.json`，升级后若群列表为空，可在目标群重新执行 `/steam addid`，让新版按群建立持久化数据。

## 9. 启动与升级验证

恢复代码、依赖、配置和数据后，启动或重新加载 AstrBot，并观察日志。

建议执行以下源码检查：

```powershell
python -m unittest discover -s tests -v
python -m compileall main.py src tests
git rev-parse --short HEAD
```

完整插件测试需要 AstrBot 环境；不依赖 AstrBot 的单元测试可直接运行。

升级成功应满足：

1. Git HEAD 为 `4f22bc5`；
2. 插件能够被 AstrBot 正常加载；
3. 原有群组、玩家、绑定和排行数据仍存在；
4. `/steam list` 能返回当前群玩家状态；
5. 开始游戏、结束游戏和成就通知符合配置；
6. 排行查询和每日排行测试推送正常；
7. AstrBot Plugin Pages 中可以打开管理页面；
8. 管理页写操作后统计数据能够立即刷新；
9. 日志中没有持续出现导入、依赖、数据解析或 API 请求异常。

## 10. 新版快速开始

1. 在 AstrBot 插件配置页面填写 `steam_api_key`；
2. 在目标群添加玩家：

   ```text
   /steam addid 76561198000000000
   ```

3. 添加成功后监控通常会自动启动，也可以手动开启：

   ```text
   /steam on
   ```

4. 查看当前群玩家状态：

   ```text
   /steam list
   ```

5. 检查最近 7 天排行：

   ```text
   /steam rank week
   ```

6. 如需每天自动推送排行：

   ```text
   /steam rank_on
   ```

指令前缀由 AstrBot 决定，本文使用默认前缀 `/`。修改、启停、跨群操作和清理指令通常需要管理员权限。

## 11. 新版指令说明

### 11.1 玩家与绑定

```text
/steam addid 76561198000000000
/steam addid 12345678
/steam addid https://steamcommunity.com/id/example
/steam addid 76561198000000000 @用户 备注名
/steam delid 76561198000000000
/steamwho @用户
/在干嘛 @用户
```

支持的 Steam 输入包括：

- 17 位 SteamID64；
- 好友码；
- Steam 个人资料链接；
- 自定义 ID 链接。

多个 ID 可使用中文或英文逗号分隔。`/steam delid` 可追加群号，由管理员执行跨群删除。

### 11.2 状态、排行与成就

```text
/steam list
/steam alllist
/steam alllist text
/steam rank
/steam rank week
/steam rank month
/steam rank 14
/steam allrank 7
/steam achievement_on
/steam achievement_off
```

排行支持今日、最近 7 天、最近 30 天或自定义天数。每日统计以凌晨 `04:00` 为边界。

### 11.3 每日排行推送

```text
/steam rank_on
/steam rank_on all
/steam rank_on list
/steam rank_on test
/steam rank_on del
/steam rank_on del 群号
```

- `/steam rank_on`：为当前群开启分群排行推送；
- `/steam rank_on all`：为当前群开启全局排行推送；
- `/steam rank_on list`：查看排行推送配置；
- `/steam rank_on test`：立即尝试推送昨日排行；
- `/steam rank_on del`：删除排行推送配置。

### 11.4 联动推送

同一个 SteamID 可以向其他群同步状态通知，无需重复轮询：

```text
/steam push_group 76561198000000000
/steam delpush_group 76561198000000000
```

应在需要接收同步通知的群中执行相关指令。

### 11.5 配置与维护

```text
/steam config
/steam set fixed_poll_interval 600
/steam on
/steam off
/steam rs
/steam清除缓存
/steam clear_groupids 群号
/steam clear_allids
/steam openbox SteamID
/steam help
```

注意事项：

- `/steam config` 显示配置时会隐藏 API Key；
- `/steam rs`、`/steam clear_groupids` 和 `/steam clear_allids` 会修改或清理运行数据，应先备份；
- `/steam openbox SteamID` 用于查看 Steam API 原始信息，输出可能较长；
- `test_*_render` 系列指令仅用于管理员排查图片渲染问题。

## 12. 管理页面使用

新版管理界面通过 AstrBot Plugin Pages 提供。插件加载成功后，在 AstrBot 管理面板的插件页面入口打开，不需要单独运行 Web 服务。

管理页面包含：

- Dashboard 汇总统计；
- 游戏时长排行榜图片；
- 游玩甘特图；
- 全局与单玩家热力图；
- 群组及群玩家管理；
- 玩家批量导入；
- 用户绑定新增、更新和删除；
- 每日排行时间、目标群和统计范围设置；
- 插件配置读取与更新；
- 插件指令权限管理；
- 玩家搜索、头像、详情和游戏封面；
- Steam API、Steam Store、SteamGridDB、封面及指定 SteamID 的连通性测试。

管理页读取 API Key 时会进行隐藏处理。写入配置、群组、玩家或绑定后，相关统计缓存会失效并重新计算，正常情况下无需等待缓存自然过期。

如果页面无法打开，应依次检查：

1. AstrBot 是否支持并启用了 Plugin Pages；
2. 插件是否成功加载；
3. `pages/steam-monitor/` 是否完整部署；
4. 浏览器开发者工具中是否存在静态资源或 API 请求错误；
5. AstrBot 日志中是否出现 `register_web_api` 或管理接口异常。

## 13. 主要配置项

### API 与网络

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `steam_api_key` | 空 | Steam Web API Key，正常查询所必需。 |
| `sgdb_api_key` | 空 | SteamGridDB API Key，用于补充游戏封面。 |
| `enable_proxy` | `false` | 是否让插件网络请求使用代理。 |
| `proxy_url` | 空 | HTTP、HTTPS 或 SOCKS5 代理地址。 |
| `retry_times` | `3` | Steam API 请求失败后的重试次数。 |

### 轮询与通知

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `fixed_poll_interval` | `0` | 固定轮询秒数；`0` 表示使用智能轮询。 |
| `smart_poll_intervals` | `1,3,5,10,20,30` | 不同活跃状态对应的轮询分钟数。 |
| `max_group_size` | `20` | 单群最多监控人数。 |
| `detailed_poll_log` | `true` | 是否输出详细轮询日志。 |
| `enable_achievement_poll` | `true` | 是否轮询并推送成就。 |
| `enable_game_start_notify` | `true` | 是否发送开始游戏通知。 |
| `enable_game_end_notify` | `true` | 是否发送结束游戏通知。 |
| `enable_network_fluctuation_notify` | `true` | 是否发送网络波动文本提醒。 |
| `notify_send_image` | `true` | 是否发送图片通知。 |
| `notify_send_text` | `true` | 是否发送文本通知。 |

### 缓存、过滤与排行

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `cache_avatar_hours` | `24` | 头像缓存时间，`0` 表示永不过期。 |
| `cache_avatar_frame_hours` | `720` | 头像框缓存时间。 |
| `cache_cover_vertical_hours` | `0` | 竖版封面缓存时间，`0` 表示永不过期。 |
| `cache_cover_horizontal_hours` | `0` | 横版封面缓存时间，`0` 表示永不过期。 |
| `game_filter_mode` | `全部游戏` | 可选 `全部游戏`、`白名单` 或 `黑名单`。 |
| `game_filter_ids` | 空 | 逗号分隔的 Steam AppID。 |
| `rank_push_hour` | `8` | 每日排行推送小时。 |
| `rank_push_minute` | `30` | 每日排行推送分钟。 |

配置项标注需要重启 AstrBot 生效时，修改后应重新加载插件或重启 AstrBot。

## 14. 数据文件与保留周期

插件数据通常位于 AstrBot 提供的 `data/steam_status_monitor/` 目录。

| 文件模式 | 内容 |
| --- | --- |
| `steam_groups.json` | 各群监控的 SteamID 列表。 |
| `bind_data.json` | 聊天平台用户、SteamID 和备注名绑定。 |
| `notify_sessions.json` | 各群 AstrBot 通知会话。 |
| `push_groups.json` | SteamID 对应的联动推送群。 |
| `rank_push_groups.json` | 每日排行目标群及统计模式。 |
| `play_records.json` | 按日期、玩家和游戏汇总的游玩分钟数。 |
| `session_records.json` | 单次游戏会话，用于甘特图和热力图。 |
| `group_<群号>_states.json` | 分群玩家最后状态。 |
| `group_<群号>_start_play_times.json` | 分群玩家各游戏开始时间。 |
| `group_<群号>_last_quit_times.json` | 分群玩家最后退出时间。 |
| `group_<群号>_pending_logs.json` | 分群待处理日志。 |
| `group_<群号>_pending_quit.json` | 分群延迟确认的退出状态。 |
| `group_<群号>_recent_games.json` | 分群最近游戏记录。 |

数据保留规则：

- `play_records.json` 保存时清理超过 30 天的数据；
- `session_records.json` 保存时清理超过 90 天的数据。

该目录可能包含群号、用户绑定、SteamID、通知会话和活动记录。虽然 `/data/steam_status_monitor/` 已加入 `.gitignore`，仍应限制访问权限并单独备份。

## 15. 常见问题排查

### 插件无法加载

检查：

- AstrBot 版本是否不低于 `4.24.2`；
- `src/` 是否完整；
- 依赖是否安装到 AstrBot 使用的 Python 环境；
- 日志中是否出现 `ModuleNotFoundError` 或语法错误。

可执行：

```powershell
python -m pip install -r requirements.txt
python -m compileall main.py src tests
```

### 升级后没有玩家

检查 `data/steam_status_monitor/steam_groups.json` 是否已恢复，以及 AstrBot 是否指向正确的数据目录。如果旧版只配置了 `steam_ids`，可在目标群重新执行：

```text
/steam addid <SteamID/好友码/链接>
```

### Steam API 查询失败

检查：

- `steam_api_key` 是否有效；
- Steam 资料隐私设置是否允许查询；
- 当前网络是否能访问 Steam Web API、Store API 和社区资源；
- 代理开关与 `proxy_url` 是否匹配；
- 管理页面中的连通性测试结果。

### 能查状态但不发送通知

检查：

- 当前群监控是否开启；
- `notify_sessions.json` 是否包含有效会话；
- 开始、结束、成就通知开关；
- `notify_send_image` 和 `notify_send_text` 是否至少开启一项；
- 游戏过滤模式是否排除了当前 AppID；
- AstrBot 机器人是否仍有目标群的发言权限。

### 管理页数据没有刷新

目标版本 `4f22bc5` 已修复写操作后统计缓存未同步失效的问题。先确认 HEAD；若不是目标版本，应更新代码。若已经是目标版本，可强制刷新浏览器并检查管理 API 日志。

### 图片渲染失败

检查 `assets/` 是否完整、字体文件是否可读、缓存目录是否可写，以及日志中的 Pillow 或下载异常。必要时使用管理员的 `test_*_render` 指令定位具体渲染环节。

## 16. 回滚方法

如果升级后出现无法接受的问题：

1. 停止 AstrBot；
2. 备份升级后产生的数据，便于后续排查；
3. 恢复升级前的完整插件代码目录；
4. 恢复升级前的插件配置；
5. 恢复升级前的 `data/steam_status_monitor/` 快照；
6. 启动 AstrBot 并验证旧版功能。

如果使用 Git，可在独立目录检出旧版本：

```powershell
git clone https://github.com/OLRainM/astrbot_plugin_steam_status_monitor.git steam-monitor-rollback
Set-Location steam-monitor-rollback
git checkout 9178f3c
```

不建议只回退代码而继续使用已被新版写入的数据。可靠回滚应同时恢复旧代码、旧配置和升级前的数据快照。

## 17. 升级检查清单

### 升级前

- [ ] AstrBot 版本不低于 `4.24.2`；
- [ ] 已停止 AstrBot；
- [ ] 已备份旧版完整代码；
- [ ] 已备份真实插件配置；
- [ ] 已备份 `data/steam_status_monitor/`；
- [ ] 已记录 Steam API Key、代理和通知设置。

### 部署时

- [ ] 已从 Fork 仓库获取 `main`；
- [ ] `main.py`、`src/`、`assets/` 和 `pages/` 已完整部署；
- [ ] 依赖已安装到 AstrBot 的 Python 环境；
- [ ] 真实配置和数据已恢复；
- [ ] 新旧插件没有同时运行。

### 启动后

- [ ] `git rev-parse --short HEAD` 输出 `4f22bc5`；
- [ ] 插件加载日志正常；
- [ ] `/steam list` 可用；
- [ ] 原群组、玩家和绑定仍存在；
- [ ] 状态通知和成就通知符合配置；
- [ ] 排行查询与测试推送正常；
- [ ] 管理页面可打开；
- [ ] 管理页写操作后统计立即更新；
- [ ] 日志中无持续异常。
