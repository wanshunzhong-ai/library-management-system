# 图书管理系统 · PyCharm 部署指南（零基础版）

> 面向**从没配过 Python 项目**的使用者。照着做即可：从装软件到浏览器里打开系统，全程图形界面 + 复制粘贴命令。
> 全程约 20–30 分钟（下载安装占大半）。
> 项目：`D:/Work/Library Management`（Python + Flask + SQLite，单文件数据库，**不需要**安装 MySQL 等数据库服务）

---

## 目录

- [第 0 步　先认识这个项目](#第-0-步先认识这个项目)
- [第 1 步　检查电脑里有没有 Python](#第-1-步检查电脑里有没有-python)
- [第 2 步　安装 PyCharm](#第-2-步安装-pycharm)
- [第 3 步　把项目放到电脑上](#第-3-步把项目放到电脑上)
- [第 4 步　用 PyCharm 打开项目](#第-4-步用-pycharm-打开项目)
- [第 5 步　配置 Python 解释器（虚拟环境）](#第-5-步配置-python-解释器虚拟环境)
- [第 6 步　安装依赖](#第-6-步安装依赖)
- [第 7 步　创建配置文件 .env（可选但推荐）](#第-7-步创建配置文件-env可选但推荐)
- [第 8 步　初始化数据库与演示数据](#第-8-步初始化数据库与演示数据)
- [第 9 步　创建"运行配置"并启动](#第-9-步创建运行配置并启动)
- [第 10 步　在浏览器里使用系统](#第-10-步在浏览器里使用系统)
- [第 11 步　登录后第一件事：改掉默认密码](#第-11-步登录后第一件事改掉默认密码)
- [进阶 1　改完代码不生效？重启一下](#进阶-1改完代码不生效重启一下)
- [进阶 2　用断点调试](#进阶-2用断点调试)
- [进阶 3　跑自动化测试](#进阶-3跑自动化测试)
- [排错手册](#排错手册)
- [附录 A　环境变量速查表](#附录-a环境变量速查表)
- [附录 B　项目结构说明](#附录-b项目结构说明)
- [附录 C　备份与恢复](#附录-c备份与恢复)
- [附录 D　部署到服务器给别人访问](#附录-d部署到服务器给别人访问)
- [附录 E　一页版检查清单](#附录-e一页版检查清单)

---

## 第 0 步　先认识这个项目

开始之前，先记住 5 件事，后面不慌：

| 项 | 内容 |
| --- | --- |
| 项目类型 | Web 应用（浏览器访问），不是双击就能开的桌面软件 |
| 技术栈 | Python + Flask + SQLite（数据库就是一个文件，**不用装数据库服务**） |
| 依赖 | **只有一个**：Flask。所以安装依赖非常快 |
| 数据放在哪 | 项目目录下的 `data/library.db`（删了就等于清空所有数据） |
| 启动后访问 | 默认 `http://127.0.0.1:8000` |

三个入口（登录后自动分流）：

| 地址 | 谁用 | 说明 |
| --- | --- | --- |
| `http://127.0.0.1:8000/` | 所有人 | 首页 = 统一登录页 + 图书馆介绍 + 免登录馆藏检索 |
| `http://127.0.0.1:8000/reader` | 读者 | 我的借阅、预约、滞纳金、消息中心、电子借书证 |
| `http://127.0.0.1:8000/admin` | 馆员 / 管理员 | 后台控制台（图书 / 读者 / 借阅 / 盘点 / 采编…） |

默认账号（第一次启动自动创建）：

| 身份 | 账号 | 密码 | 备注 |
| --- | --- | --- | --- |
| 系统管理员 | `admin` | `admin123` | **登录后必须改** |
| 读者 | 借书证号，如 `8809001` | 证号**后 6 位**，如 `809001` | 首次登录会强制改密 |
| 流通馆员 | 由管理员在「账号管理」里创建 | 创建时自定 | 权限比管理员少（不能管账号、看日志、审批） |

---

## 第 1 步　检查电脑里有没有 Python

这个项目需要 **Python 3.10 或更高版本**（推荐 3.11 / 3.12 / 3.13）。

### 1.1 打开"终端"

- **Windows**：按 `Win + R`，输入 `cmd` 回车（或开始菜单搜索"命令提示符"）。
- **macOS**：按 `Command + 空格`，输入 `终端` 回车。

### 1.2 输入检查命令

```bash
python --version
```

或者（macOS / 部分 Windows 环境）：

```bash
python3 --version
```

**看到什么算通过**：类似 `Python 3.12.4` 的输出（版本号 ≥ 3.10）。

### 1.3 如果没装或版本太低

去官网下载安装：<https://www.python.org/downloads/>

安装时**务必勾选**（Windows 安装界面最下方）：

```
[√] Add python.exe to PATH
```

不勾这一项，后面所有 `python` 命令都会提示"不是内部或外部命令"。

安装完**关掉终端重新打开**，再执行一次 `python --version` 确认。

---

## 第 2 步　安装 PyCharm

本项目用 **PyCharm Community（社区版，免费）** 就完全够用。

1. 打开 <https://www.jetbrains.com/pycharm/download/>
2. 找到 **Community** 版本（免费），点 **Download**
3. 运行安装包，一路 **Next**，建议勾选：
   - Windows：`Create Desktop Shortcut`（创建桌面图标）、`Add "bin" folder to the PATH`
4. 安装完成后**先不要**急着打开项目，继续下一步。

> 已有专业版（Professional）也可以，操作路径完全一样。

---

## 第 3 步　把项目放到电脑上

### 情形 A：项目已经在你的电脑上

确认项目文件夹存在，例如 `D:\Work\Library Management`。
**注意**：路径里可以有空格（本项目就是），但**尽量不要用中文路径**，某些 Python 工具链对中文路径支持不佳。

### 情形 B：需要从压缩包解压

解压到一个**你记得住**的位置，例如：

```
D:\Work\Library Management
```

### 判断"放对了"的条件

打开这个文件夹，应当能直接看到以下内容：

```
app.py              ← 启动入口
config.py           ← 配置解析
requirements.txt    ← 依赖清单
library\            ← 业务代码（路由/服务/仓储三层）
templates\          ← 页面模板
static\             ← 前端 JS / CSS
tests\              ← 自动化测试
data\               ← 数据库目录（首次启动后会自动出现 library.db）
```

**不要**把项目塞到 `C:\Program Files`、`C:\Windows` 这类系统目录里（会因权限问题写不进数据库）。

---

## 第 4 步　用 PyCharm 打开项目

1. 启动 PyCharm
2. 欢迎界面点 **Open**（打开）
   - 如果已经打开了别的项目：顶部菜单 **File → Open**
3. 在弹出的文件选择框里，**选中项目文件夹本身**（`Library Management` 这个文件夹），点 **OK / Open**
4. 若弹出 "Trust and Open Project ...?"（是否信任项目），选 **Trust Project**
   - 这是 PyCharm 的安全询问，本地项目放心点信任，否则代码不能运行

打开后，左侧 **Project（项目）** 面板里能看到 `app.py`、`library`、`templates` 等，就对了。

> **看不到某个文件？** 右键项目名 → **Reload from Disk**（从磁盘重新加载）。

---

## 第 5 步　配置 Python 解释器（虚拟环境）

**"解释器"就是"用哪个 Python 来跑这个项目"。** 我们给它单独建一个环境（叫虚拟环境），
好处是：项目装的库不会污染你电脑里其它 Python 项目。

### 5.1 打开设置

- Windows：**File → Settings**（快捷键 `Ctrl + Alt + S`）
- macOS：**PyCharm → Settings / Preferences**（快捷键 `Command + ,`）

### 5.2 找到解释器设置

左侧依次展开：

```
Project: Library Management  →  Python Interpreter
```

### 5.3 情况一：项目里已经有 `.venv` 文件夹（推荐直接用它）

如果项目目录下已经存在 `.venv` 文件夹（说明别人已经配好了）：

1. 点右侧 **Add Interpreter → Add Local Interpreter…**
2. 左侧选 **Virtualenv Environment → Existing**（已存在）
3. **Interpreter** 一栏点 `...` 按钮，选中：
   - Windows：`.venv\Scripts\python.exe`
   - macOS / Linux：`.venv/bin/python`
4. 点 **OK** → **OK**

### 5.4 情况二：没有 `.venv`，从零新建（多数人走这条）

1. 点右侧 **Add Interpreter → Add Local Interpreter…**
2. 左侧保持 **Virtualenv Environment**
3. 选 **New**（新建），填两栏：

   | 栏位 | 填什么 |
   | --- | --- |
   | Location | `项目路径\.venv`（直接用默认值即可） |
   | Base interpreter | 下拉选你的 Python 3.10+（若为空，点 `...` 手动选 `python.exe`） |

4. 勾选 **Inherit global site-packages**（继承全局包）**不要勾**（保持环境干净；不勾也没问题）
5. 点 **OK**，等 PyCharm 建立环境（底部会显示进度条，十几秒）

### 5.5 确认成功

回到 **Python Interpreter** 页面，应看到类似：

```
Python 3.12  (Library Management)   D:\Work\Library Management\.venv
```

★ **如果这一步显示的是 `Python 3.x (venv)` 且路径指向项目内的 `.venv`，说明成功了。**

---

## 第 6 步　安装依赖

### 方式一：PyCharm 自动提示（最省事）

打开项目后，如果 PyCharm 顶部弹出一条黄色提示：

```
Package requirements 'requirements.txt' are not satisfied
[Install requirements]
```

直接点 **Install requirements**，等它装完即可跳到第 7 步。

### 方式二：手动在终端装（推荐，能看到结果）

1. PyCharm 底部点 **Terminal（终端）** 标签
   - 看不到？顶部菜单 **View → Tool Windows → Terminal**
2. 确认终端提示符前面有 `(.venv)` 字样（例如 `(.venv) D:\Work\Library Management>`）
   - **没有的话**，说明解释器没配对，回到[第 5 步](#第-5-步配置-python-解释器虚拟环境)重做
3. 输入并回车：

```bash
pip install -r requirements.txt
```

4. 看到最后一行是 `Successfully installed Flask-3.x.x ...` 即成功
5. 验证一下：

```bash
python -c "import flask; print(flask.__version__)"
```

能打印出 Flask 版本号（如 `3.1.0`）就没问题。

> **网络慢 / 装不上？** 用国内镜像：
> ```bash
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

---

## 第 7 步　创建配置文件 .env（可选但推荐）

项目**不配 `.env` 也能跑**（所有配置都有默认值）。但建议配一份，方便以后改端口、改馆名。

1. 在 PyCharm 左侧项目树里找到 **`.env.example`**
2. 右键 → **Copy**（复制），再在项目根目录右键 → **Paste**（粘贴），重命名为 **`.env`**
   - 更简单：在 Terminal 里执行（Windows）
     ```bash
     copy .env.example .env
     ```
     macOS / Linux：
     ```bash
     cp .env.example .env
     ```
3. 双击打开 `.env`，先只改这一项（最常用）：

```ini
# 端口被占用时改成别的，比如 8010
LIBRARY_PORT=8000
```

**小白建议**：第一次跑通之前，先别改其它值。跑通后再按[附录 A](#附录-a环境变量速查表)逐个调。

> **`.env` 会不会被提交到代码仓库？** 不会，项目已在 `.gitignore` 里忽略它。

---

## 第 8 步　初始化数据库与演示数据

第一次运行需要建表。有两种做法：

### 做法一：直接用命令（推荐）

在 PyCharm 的 **Terminal** 里执行：

```bash
python app.py init
```

看到 `数据库已初始化：...\data\library.db` 即成功。

再写入演示数据（有图书、读者、借阅，方便马上体验）：

```bash
python app.py seed
```

看到 `演示数据已写入。` 即成功。

> **不想用演示数据？** 跳过 `seed`。但空库登录后什么都是空的，不利于熟悉界面。

### 做法二：什么都不做

直接启动（下一步）——程序第一次启动时会**自动建表并创建默认管理员**，
只是没有演示数据。若想连演示数据一起，启动参数加 `--seed`（见下一步）。

**想推倒重来**（清空所有数据重建演示数据）：

```bash
python app.py reset
```

---

## 第 9 步　创建"运行配置"并启动

"运行配置"就是告诉 PyCharm："点这个绿三角，就按这样启动这个项目"。

### 9.1 打开运行配置

顶部工具栏 → 下拉框（默认可能是 `Current File`）→ 点 **Edit Configurations…**
（或菜单 **Run → Edit Configurations…**）

### 9.2 新建一个 Python 配置

1. 左上角 **`+`** → 选 **Python**
2. 按下表填写：

   | 栏位 | 填什么 | 说明 |
   | --- | --- | --- |
   | **Name** | `图书馆系统` | 随便起，能认出来就行 |
   | **Script path** | 点右侧文件夹图标，选项目里的 `app.py` | 必须选这个文件 |
   | **Parameters** | `run --seed` | 第一次跑建议带 `--seed`（已建过库就自动跳过） |
   | **Python interpreter** | 选 **Project Default** 或 `Python 3.x (Library Management)` | 必须指向第 5 步的 `.venv` |
   | **Working directory** | 项目根目录（如 `D:\Work\Library Management`） | **这一项最关键**，路径错了会找不到数据库和模板 |
   | **Emulate terminal in output console** | 不用勾 | |

3. 点 **OK**

### 9.3 启动

点顶部绿色 **▶ Run**（或按 `Shift + F10`）。

**成功的标志**——底部 Run 窗口出现：

```
  图书管理系统已启动 →  http://127.0.0.1:8000
 * Running on http://127.0.0.1:8000
 * Press CTRL+C to quit
```

★ 有这两行就成功了，**不要关掉**这个窗口（关掉就等于关掉服务）。

**停止服务**：点 Run 窗口左侧的红色 ■（Stop）按钮，或在 Run 窗口里按 `Ctrl + C`。

---

## 第 10 步　在浏览器里使用系统

打开浏览器（Chrome / Edge / Firefox 均可），地址栏输入：

```
http://127.0.0.1:8000
```

### 第一次进来看到什么

首页就是**统一登录页**，同时展示图书馆介绍、借阅规则、馆藏推荐与**免登录的馆藏检索**——随便搜个书名试试。

右上角是登录框，**同一个框自动识别身份**：

- 输 `admin` + `admin123` → 进**后台控制台**
- 输借书证号（如演示数据里的 `8809001`）+ 证号后 6 位 → 进**读者工作台**

登录后地址会自动跳到 `/admin` 或 `/reader`。

### 后台控制台逛一圈（用 admin 登录）

左侧导航从上到下依次是：

| 导航 | 能做什么 |
| --- | --- |
| 概览 | 馆藏 / 在借 / 逾期 / 读者等统计卡 + 借阅趋势图 |
| 图书管理 | 新增、编辑、上下架、导出 CSV |
| 读者管理 | 建档、冻结、重置密码、发通知、上传照片/证件 |
| 办证审核 | 审批门户提交的办证申请，通过即发证 |
| 借阅管理 | 借书（支持扫码枪）、还书、批量还书、续借、打印凭证 |
| 滞纳金管理 | 代收、减免（大额会转审批） |
| 预约管理 | 看队列、通知到书、取消 |
| 赔偿处理 | 丢书/损书登记与结案 |
| 盘点 | 出盘点单、录实盘、差异报告与调账 |
| 采编验收 | 到货登记、ISBN 去重加册或新建书目 |
| 公告管理 | 发公告、维护开馆时间 |
| 读者互动 | 审核书评 / 荐购 / 资料变更 |
| 批量导入 | 上传 CSV / Excel 批量建图书与读者 |
| 审批中心 | （管理员）批准超阈值的罚金减免 |
| 账号管理 | （管理员）建馆员账号、改角色、停用 |
| 操作日志 | （管理员）谁在什么时候改了什么 |
| 系统维护 | （管理员）后台任务、邮件渠道、数据备份 |

---

## 第 11 步　登录后第一件事：改掉默认密码

只要还在用 `admin123`：

- 启动日志会打印警告
- 后台页面顶部会**常驻一条红色提醒横幅**

修改方法：右上角头像/用户名附近 → **修改密码** → 输入旧密码与新密码 → 保存后需要重新登录。

改完横幅会消失。

---

## 进阶 1　改完代码不生效？

本项目启动时**关闭了自动重载**（`use_reloader=False`），原因是要配合后台定时扫描线程。

所以：**改完 `.py` / `.html` / `.js` / `.css` 后，需要重启服务**：

- 点 Run 窗口的 ■ 停止，再点 ▶ 启动；
- 或直接点 **Rerun**（绿色循环箭头，快捷键 `Ctrl + F5`）。

> 只改 `static/` 下的 JS / CSS 时，浏览器按 `Ctrl + F5` 强制刷新（绕过缓存）通常就够。

---

## 进阶 2　用断点调试

想看清某段逻辑怎么跑的：

1. 在代码行号左侧**点一下**，出现红点（断点），例如 `library/loans/service.py` 的借书函数
2. 点绿色 **🐞 Debug** 按钮（`Shift + F9`）启动
3. 在浏览器里触发对应操作（如借一本书）
4. PyCharm 会停住，**Debug** 面板里可以看变量值

常用快捷键：

| 操作 | Windows | macOS |
| --- | --- | --- |
| 单步跳过 | `F8` | `F8` |
| 单步进入函数 | `F7` | `F7` |
| 继续运行 | `F9` | `Command + Option + R` |
| 停止调试 | `Ctrl + F2` | `Command + F2` |

---

## 进阶 3　跑自动化测试

项目自带端到端冒烟测试，覆盖借还、预约、罚金、权限、并发、安全等。

在 Terminal 里执行：

```bash
python tests/smoke_test.py
```

成功时最后一行是：

```
结果：9xx 项通过，0 项失败
```

想跑单个对照探针（验证并发 / 安全 / 逾期口径）：

```bash
python tests/concurrency_probe.py
python tests/security_probe.py
python tests/overdue_probe.py
```

> 也可以右键 `tests/smoke_test.py` → **Run 'smoke_test'**。
> 测试会在系统临时目录建独立数据库，**不会动你的演示数据**。

---

## 排错手册

### 通用排查三步

1. 看 **Run 窗口**有没有红色报错（最有用）
2. 看 **Terminal** 里 `python --version`、`pip show flask` 是否正常
3. 看 PyCharm **右下角**是否写着 `<No interpreter>` 或多解释器混乱

---

### 问题 1：`'python' 不是内部或外部命令`

**原因**：装 Python 时没勾 "Add python.exe to PATH"。

**解决**：
- 重新运行 Python 安装包 → **Modify** → 勾上 `Add Python to environment variables`；
- 或临时用绝对路径（PyCharm 的 Terminal 一般不会有这个问题，因为它用的是虚拟环境）。

---

### 问题 2：`ModuleNotFoundError: No module named 'flask'`

**原因**：依赖装到了别的 Python 上，或者解释器没选对。

**解决**：
1. 确认 PyCharm Terminal 提示符前有 `(.venv)`
2. 重装依赖：`pip install -r requirements.txt`
3. 确认 **Settings → Project → Python Interpreter** 指向 `.venv`
4. 仍然不行：`python -m pip install flask`（明确用当前解释器装）

---

### 问题 3：`OSError: [WinError 10048] 通常每个套接字地址只允许使用一次`

**原因**：**端口 8000 被占用**（常见：上次的服务窗口没关干净）。

**解决（任选一种）**：
- 找到并关掉之前残留的 Run 窗口 / 命令提示符窗口，再启动；
- 换端口：在 `.env` 里改 `LIBRARY_PORT=8010`，然后访问 `http://127.0.0.1:8010`；
- 命令行查占用进程并结束（Windows）：
  ```bash
  netstat -ano | findstr :8000
  ```
  记下最后一列 PID，然后：
  ```bash
  taskkill /PID 前面查到的数字 /F
  ```

---

### 问题 4：`sqlite3.OperationalError: database is locked`

**原因**：同时跑了两个进程在写同一个库（例如一边开着服务一边跑 `reset`），或数据库文件被别的软件（如 DB Browser）独占。

**解决**：
- 先**停止服务**，再执行 `python app.py reset` 之类的写命令，然后再启动；
- 关掉正在打开 `data/library.db` 的第三方工具。

---

### 问题 5：网页能打开，但**样式全乱 / 图片不显示**

**解决**：
1. 浏览器按 `Ctrl + F5` 强制刷新（清缓存）
2. 确认访问的是 `http://127.0.0.1:8000`，**不是**双击 `templates/index.html` 打开的本地文件
   （直接双击 HTML 文件是打不开的，必须通过服务访问）

---

### 问题 6：页面中文变乱码

**解决**：浏览器编码保持默认（UTF-8）；导出的 CSV 用 Excel 打开时若乱码，
说明文件被改了编码——本项目导出的 CSV 自带 BOM，直接用 Excel / WPS 双击打开即可。
若你手工改过文件，请用 **UTF-8（无 BOM 或带 BOM 均可）** 保存。

---

### 问题 7：启动后**立刻退出**，Run 窗口显示 `启动失败：...`

**原因**：配置非法或数据库路径不可写。

**解决**：
- 看报错信息里点名的变量名（例如 `LIBRARY_PORT 必须是整数`），去 `.env` 改正；
- 若提示数据库无法打开：确认项目**不在** `C:\Program Files` 等需要管理员权限的目录下。

---

### 问题 8：`sqlite3.OperationalError: unable to open database file`

**原因**：`data` 目录不存在或没有写权限。

**解决**：
- 在项目根目录手动新建 `data` 文件夹，再重试；
- 或把项目移到用户目录下（如 `D:\Work\...`）。

---

### 问题 9：演示数据没写进去 / 登录提示"账号或密码错误"

**原因**：`seed` 在库非空时会跳过。

**解决**：
- 用 `python app.py seed --force` 强制重写；
- 或 `python app.py reset` 清空重建；
- 管理员账号密码一定是 `admin` / `admin123`（除非你改过；改过就用新密码）。

---

### 问题 10：读者的初始密码是多少？

**规则**：**借书证号的后 6 位**。例如证号 `8809001` → 密码 `809001`；证号 `2024001` → 密码 `024001`。

读者首次登录会被**强制改密**，改密前除了登录/登出/改密之外什么也做不了（这是设计）。

忘了密码：后台「读者管理」→ 找到该读者 → **重置密码**（重置后又回到"证号后 6 位"）。

---

### 问题 11：上传读者照片 / 证件失败

**检查**：
- 格式必须是 **JPEG / PNG / WebP / PDF**（改扩展名伪造是没用的，服务端会读文件头校验）
- 大小是否超过上限（默认 **4MB**，可改 `LIBRARY_UPLOAD_MAX_MB`）
- 确认 `uploads/` 目录可写（默认自动创建在 `data/` 同级）

---

### 问题 12：收不到邮件提醒

邮件是**可选**渠道，默认**没有配置**。配置步骤：

1. 在 `.env` 里填 SMTP（以 163 邮箱为例）：
   ```ini
   LIBRARY_SMTP_HOST=smtp.163.com
   LIBRARY_SMTP_PORT=465
   LIBRARY_SMTP_USER=你的邮箱@163.com
   LIBRARY_SMTP_PASSWORD=邮箱的授权码（不是登录密码）
   LIBRARY_SMTP_FROM=你的邮箱@163.com
   LIBRARY_SMTP_SECURE=ssl
   ```
2. 重启服务
3. 用 **admin** 登录 → **系统维护 → 邮件渠道** → 填收件邮箱 → 点「发送测试邮件」
4. 收到测试邮件即配置成功；没收到就看该页的「待发送 / 发送失败」列表与错误信息

**注意**：只有读者**留了邮箱且没退订**才会发邮件；没配置 SMTP 时消息只进站内消息中心，不会丢。

---

### 问题 13：后台任务没在跑 / 数据没自动更新

查看 **系统维护 → 后台任务**：

- 「后台线程：运行中，每 600 秒一轮」→ 正常
- 「未启用」→ 检查 `.env` 的 `LIBRARY_SWEEP_INTERVAL_SECONDS` 是否为 `0`（0 = 关闭自动扫描）
- 想立刻看效果：点 **立即执行一轮**，下方会多一条"手动触发"的记录

---

### 问题 14：`pkg_resources` / `AttributeError` 之类的怪报错

**原因**：解释器版本过低（如 3.8）。

**解决**：换 **Python 3.10+** 重建虚拟环境（第 5 步），重新装依赖。

---

## 附录 A　环境变量速查表

配置写在项目根目录的 `.env` 里（由 `.env.example` 复制而来），改完**重启生效**。

### 最常改的

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LIBRARY_PORT` | `8000` | 端口，被占用就改 |
| `LIBRARY_HOST` | `127.0.0.1` | 监听地址；局域网内其他电脑要访问就改 `0.0.0.0` |
| `LIBRARY_DB_PATH` | `./data/library.db` | 数据库文件位置 |
| `LIBRARY_NAME` | `星海图书馆` | 网站标题里的馆名 |
| `LIBRARY_SLOGAN` | `让每一本书都找到它的读者` | 首页标语 |
| `LIBRARY_ADMIN_PASSWORD` | `admin123` | 仅**首次**创建管理员时生效，之后以数据库为准 |

### 借阅规则

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LIBRARY_LOAN_DAYS` | `30` | 单次借期（天） |
| `LIBRARY_MAX_RENEW` | `2` | 最多续借次数 |
| `LIBRARY_FINE_PER_DAY` | `0.50` | 逾期罚金（元/天） |
| `LIBRARY_FINE_MAX_DAYS` | `365` | 计息天数上限（0 = 不限） |
| `LIBRARY_FINE_CAP` | `50` | 单笔罚金上限（元，0 = 不限） |
| `LIBRARY_DEFAULT_MAX_BORROW` | `5` | 新读者默认可借册数 |
| `LIBRARY_RESERVATION_HOLD_DAYS` | `3` | 预约到书后的取书保留天数 |
| `LIBRARY_NOTIFY_DUE_SOON_DAYS` | `3` | 到期前多少天提醒（0 = 关闭） |

### 后台任务 / 文件 / 内控

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LIBRARY_SWEEP_INTERVAL_SECONDS` | `600` | 后台扫描间隔（秒）；`0` = 不启动后台线程 |
| `LIBRARY_UPLOAD_DIR` | 空 | 附件目录；留空 = `data/` 同级的 `uploads/` |
| `LIBRARY_UPLOAD_MAX_MB` | `4` | 单个附件大小上限（MB） |
| `LIBRARY_SMTP_HOST` | 空 | 邮件服务器；**留空 = 不发邮件** |
| `LIBRARY_SMTP_PORT` | `0` | `0` = 按加密方式自动（SSL 465 / TLS 587） |
| `LIBRARY_SMTP_USER` / `LIBRARY_SMTP_PASSWORD` | 空 | 邮箱账号与授权码 |
| `LIBRARY_SMTP_SECURE` | `auto` | `auto` / `ssl` / `tls` / `none` |
| `LIBRARY_APPROVAL_WAIVER_THRESHOLD` | `0` | 罚金减免达到该金额（元）转审批；`0` = 不启用 |
| `LIBRARY_BACKUP_DIR` | 空 | 备份目录；留空 = `data/` 同级的 `backups/` |

### 安全相关（生产环境务必看）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LIBRARY_DEBUG` | `false` | 生产环境必须 `false` |
| `LIBRARY_SECRET_KEY` | 空 | 留空会自动生成并保存；显式配置须 ≥16 位随机串 |
| `LIBRARY_COOKIE_SECURE` | `false` | 上了 HTTPS 才改 `true` |
| `LIBRARY_SESSION_HOURS` | `8` | 登录有效期（小时） |
| `LIBRARY_LOGIN_MAX_ATTEMPTS` | `5` | 连续登录失败锁定阈值 |
| `LIBRARY_TRUSTED_PROXIES` | 空 | 有反向代理时才填代理 IP / CIDR |

> 完整清单见项目里的 `.env.example`，每一项都有中文注释。

---

## 附录 B　项目结构说明

```
Library Management/
├── app.py                    # 启动入口 + 命令行（run / init / seed / reset / check / repair / backup / restore）
├── config.py                 # 配置解析（读 .env / 环境变量，含合法性校验）
├── requirements.txt          # 依赖清单（只有 Flask）
├── .env.example              # 配置模板
├── .env                      # 你自己的配置（由上面复制，不要提交）
│
├── library/                  # 业务代码：每个模块都是「routes → service → repository」三层
│   ├── web.py                # 应用工厂、蓝图注册、全局鉴权守卫
│   ├── db.py                 # 建表 / 索引 / 增量迁移 / 连接管理
│   ├── books/ members/ loans/          # 三大业务域
│   ├── reservations/ fines/            # 预约、滞纳金
│   ├── compensations/ stocktaking/     # 赔偿、盘点
│   ├── acquisitions/ importer/         # 采编验收、批量导入
│   ├── memberfiles/ notifications/     # 读者附件、站内通知 + 邮件出站
│   ├── approvals/                      # 审批工单
│   ├── sysadmin/                       # 系统维护（任务 / 邮件渠道 / 备份）
│   ├── applications/ engagement/       # 办证审核、读者互动
│   ├── announcements/ accounts/ public/ reader/ dashboard/
│   ├── auth/                 # 会话、密码哈希、角色与能力（权限矩阵在 auth/permissions.py）
│   ├── scheduler.py          # 后台定时扫描线程
│   ├── backup.py             # 备份 / 恢复
│   └── audit.py errors.py validation.py timeutil.py
│
├── templates/                # 页面模板
│   ├── index.html            # 后台控制台（单页 + hash 路由）
│   ├── reader.html           # 读者工作台
│   └── public.html           # 首页（统一登录 + 馆藏检索）
├── static/                   # 前端资源
│   ├── js/app.js reader.js public.js   # 三个端各自的脚本
│   ├── css/                            # 样式
│   └── js/qrcode.js barcode.js         # 纯前端二维码 / 条形码（无第三方依赖）
│
├── data/                     # 运行后生成
│   ├── library.db            # 全部数据就在这一个文件里
│   ├── uploads/              # 读者照片 / 证件
│   └── backups/              # 备份文件
│
├── tests/                    # 端到端测试与对照探针
│   ├── smoke_test.py
│   ├── concurrency_probe.py security_probe.py overdue_probe.py
│
└── docs/                     # 项目文档
    ├── 优化评审.md            # 各批优化的评审与验收记录
    ├── 身份功能清单.md         # 四类身份的功能现状与缺口
    ├── 身份闭环检查.md         # 每个身份的链路闭环复核
    └── PyCharm部署指南.md      # 本文档
```

**改代码时的一条铁律**：新增后台接口必须放在 `/api/` 前缀下（不要放 `/api/reader/` 或 `/api/public/`），
否则会吃到错误的鉴权分支（读者会话 / 免登录）。

---

## 附录 C　备份与恢复

### 做备份（可热备，不用停服）

在 PyCharm Terminal 里：

```bash
# 只备份数据库
python app.py backup

# 连读者附件一起打包成 zip（整站迁移用这个）
python app.py backup --with-uploads

# 查看已有备份
python app.py backup --list
```

备份文件落在 `data/backups/`，文件名形如 `library-20261012-093000.db`。

也可以在后台做：**系统维护 → 数据备份 → 立即备份**，然后点「下载」保存到别处。

### 恢复（必须停服）

```bash
# 1. 先停掉服务（点 PyCharm 的 Stop）
# 2. 执行恢复（覆盖前会自动留一份安全副本）
python app.py restore --from data/backups/library-20261012-093000.db
# 3. 重新启动服务
```

**建议**：每天营业前点一次「立即备份」；做批量导入、盘点调账、批量删除这类大动作之前也备一次。

---

## 附录 D　部署到服务器给别人访问

PyCharm 只适合**开发 / 自己电脑上跑**。要让同事、读者在别的电脑上访问，需要放到服务器：

### 1. 让局域网内可访问（最简单）

改 `.env`：

```ini
LIBRARY_HOST=0.0.0.0
LIBRARY_PORT=8000
```

重启后，同网段其他电脑用 `http://你的电脑IP:8000` 访问。
（Windows 首次会弹防火墙提示，勾选"专用网络"允许。）

> 注意：`0.0.0.0` 意味着同网络里任何人都能访问，请务必先改掉默认管理员密码。

### 2. 正式部署（可选）

项目自带的是 Flask 开发服务器，**不适合生产**。生产建议用 WSGI 服务器。项目已提供零参工厂
`app:create_app_for_serve`（等价于 `python app.py run`，会读配置并启动后台扫描）。

Windows 上用 waitress：

```bash
pip install waitress
waitress-serve --call --host=0.0.0.0 --port=8000 app:create_app_for_serve
```

Linux 上常用 gunicorn：

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8000 "app:create_app_for_serve()"
```

> 多进程部署时，后台扫描线程会每个进程各跑一次——扫描逻辑是幂等的（重复执行只是几次空查询），
> 若要严格只跑一次，把 `LIBRARY_SWEEP_INTERVAL_SECONDS=0` 关掉线程，改用系统的定时任务
> （cron / 计划任务）定期执行 `python app.py check` 之类的维护动作。

### 3. 上 HTTPS

放在 Nginx / Caddy 后面做反向代理并配证书，然后：

```ini
LIBRARY_COOKIE_SECURE=true
LIBRARY_TRUSTED_PROXIES=127.0.0.1
```

`LIBRARY_TRUSTED_PROXIES` 不填的话，反向代理后面的限流会失效（所有人算同一个 IP）。

---

## 附录 E　一页版检查清单

从零到跑通，按顺序勾完即可：

- [ ] `python --version` → 显示 3.10 或更高
- [ ] 装好 PyCharm Community，并 **Trust Project**
- [ ] PyCharm 里选中项目文件夹（能看到 `app.py`）
- [ ] **Settings → Project → Python Interpreter** 指向项目内的 `.venv`
- [ ] Terminal 提示符前有 `(.venv)`
- [ ] `pip install -r requirements.txt` 成功
- [ ] `python -c "import flask; print(flask.__version__)"` 有输出
- [ ] （可选）复制 `.env.example` 为 `.env`
- [ ] `python app.py init` 成功
- [ ] `python app.py seed` 成功（想要演示数据）
- [ ] 新建 Run Configuration：Script = `app.py`，Working directory = 项目根
- [ ] 点 ▶ 启动，看到 `Running on http://127.0.0.1:8000`
- [ ] 浏览器打开 `http://127.0.0.1:8000`，用 `admin` / `admin123` 登录
- [ ] **改掉默认管理员密码**
- [ ] 用 `8809001` / `809001` 登录读者端，逛一圈「我的借阅」
- [ ] 跑一次 `python tests/smoke_test.py`，确认 0 项失败

---

**遇到本文档没覆盖的问题**：先看 Run 窗口的最后 20 行报错——90% 的问题（少了依赖、端口占用、路径不对、配置非法）都能从那里直接看出原因。
