# Imagefree Skill


## 安装

复制 `skills/imagefree` 整个文件夹到你的 Agent 支持的 skills 目录。入口是
`SKILL.md`，脚本也能脱离 Agent 独立运行。支持 Python 3.10+、Windows/macOS/Linux。

```sh
git clone https://github.com/sl251/imagefree-skill.git
cd imagefree-skill
python -m pip install -r skills/imagefree/requirements-browser.txt
python -m playwright install chromium
python skills/imagefree/scripts/generate.py "湖边的小木屋，水彩画" --size wide
```

已有 Chrome/Edge/Chromium 时通常无需下载 Chromium；也可以传入
`--browser-executable`。仅运行 HTTP 时不必安装第三方依赖。

## 用法

在支持的 Agent 中请求：“用 imagefree 生成一张湖边木屋的水彩画，16:9”。

```sh
# 自动模式：HTTP → 浏览器
python skills/imagefree/scripts/generate.py "A cabin beside a lake" --size wide

# 复杂提示词放入 UTF-8 文件，避免命令行转义问题
python skills/imagefree/scripts/generate.py --prompt-file prompt.txt --output-dir ./imagefree-output

# 接着轮询原任务，不重复提交生成
python skills/imagefree/scripts/generate.py --task-id YOUR_TASK_ID

# 排查单一路径 / 统一代理
python skills/imagefree/scripts/generate.py "A cabin" --transport http
python skills/imagefree/scripts/generate.py "A cabin" --proxy http://127.0.0.1:7890
```

尺寸：`square` → 1:1，`portrait` → 3:4，`landscape` → 4:3，`tall` → 9:16，
`wide` → 16:9。支持 `--style`、`--aspect-ratio`、`--timeout`、`--headed` 等选项。
成功时 stdout 返回含绝对图片路径的 JSON；诊断写入 stderr。文件名唯一，不覆盖旧图。

HTTP 在轮询/下载阶段失败时，浏览器继续使用相同任务 ID/图片地址。明确失败、无效参数、
认证/付费拒绝、提交结果不确定等情况会停止，避免重复生成。浏览器无法保证通过站点验证；
需要人工验证时可用 `--headed --verification-wait 30`。本项目不包含验证码破解。

内部接口可能随网站变化。浏览器配置、失败恢复、输出格式与限制见
[运行说明](skills/imagefree/references/operations.md)。浏览器 profile 与生成图片不会提交到 Git。

## 验证

```sh
python -m unittest discover -s tests -v
```

设置 `IMAGEFREE_TEST_BROWSER=1` 可额外运行本地服务上的真实浏览器回退测试。
测试默认不调用线上生成接口；通过测试不代表线上服务始终可用。

