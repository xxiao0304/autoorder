# SZTU Gym Auto Booking

深圳技术大学体育馆预约自动化。当前主流程全部走接口，浏览器只用于登录态刷新。

## 当前策略

羽毛球包场：

- 每天 `17:55` 预检并刷新登录态。
- 预检会提前查询第二天 `19:00`、`20:20` 的包场聚合场次。
- 预检会调用 `assign/detail` 展开每个具体场地，并缓存具体 `sessionId` 到 `state/badminton_package_cache.json`。
- 每天 `17:59` 启动抢票进程，`18:00:00` 精准开抢。
- 抢票窗口持续到 `18:03:00`。
- `19:00` 和 `20:20` 两个时段同步开抢，不会等一个时段结束后再抢另一个。
- 开抢时优先读取缓存里的具体场地 `sessionId`。
- 默认每个时段最多持有 1 个待支付订单，避免同一账号积分被多个待支付订单提前占满。
- 如果接口响应丢失但服务端已生成待支付订单，会扫描待支付列表接管：支付一个、取消多余订单。
- 支付成功后，可按 `peer_accounts` 自动添加同行人学号、工号或手机号。
- 羽毛球可配置独立账号，和体能中心/健身房账号互不覆盖登录态。

体能中心：

- 每天 `18:59` 启动，`19:00:00` 预约第二天 `19:00`、`20:20`。
- 使用普通快速 API 策略。
- `venue_id=46`，`block_type=1`。

健身房：

- 每天 `18:59` 启动，`19:00:00` 预约第二天 `19:00`、`20:20`。
- 使用普通快速 API 策略。
- `venue_id=4`，`block_type=1`。

自动取消：

- 每小时检查一次当天订单。
- 为每个订单规划一次性取消任务。
- 取消时间为 `开场时间 + 61 分钟`。
- 到点后使用纯 API 脚本取消，不启动浏览器。
- 如果配置了羽毛球独立账号，取消规划会同时扫描主账号和羽毛球账号。

## 安装

```powershell
cd E:\code\autoorder
python -m pip install -r requirements.txt
```

首次使用时复制配置：

```powershell
Copy-Item .\config.example.json .\config.json
```

在 `config.json` 中配置：

- `login.username`
- `login.password`
- `notify.enabled`
- `notify.provider`
- `notify.serverchan_sendkey` 或 `notify.wechat_webhook_url`
- `accounts.badminton.username`
- `accounts.badminton.password`
- `accounts.badminton.storage_state`
- `automation.booking_profiles.badminton_18pm.account_profile`
- `automation.booking_profiles.badminton_18pm.max_holds_per_slot`
- `automation.booking_profiles.badminton_18pm.peer_accounts`

羽毛球使用独立账号示例：

```json
"accounts": {
  "badminton": {
    "username": "",
    "password": "",
    "storage_state": "state/storage_badminton.json"
  }
}
```

`peer_accounts` 只建议配置在羽毛球包场 profile 中，例如：

```json
"peer_accounts": [
  "2310412088"
]
```

## 登录态

```text
state/storage.json
state/storage_badminton.json
```

计划任务和手动脚本会在接口返回未登录时自动刷新登录态。

羽毛球独立账号的登录态由 `17:55` 预检任务刷新；主账号登录态由体能中心/健身房预约和取消规划脚本按需刷新。

## 安装计划任务

推荐只使用智能任务安装脚本：

```powershell
cd E:\code\autoorder
powershell -ExecutionPolicy Bypass -File .\scripts\install_smart_tasks.ps1
```

如果需要指定 Python：

```powershell
$py="E:\Users\Xiao Jie\miniconda3\python.exe"
powershell -ExecutionPolicy Bypass -File .\scripts\install_smart_tasks.ps1 -PythonPath $py
```

安装后应看到这些任务：

```text
SZTU Smart Badminton Precheck 1755
SZTU Smart Booking Badminton 18
SZTU Smart Booking FitnessCenter 19
SZTU Smart Booking GymRoom 19
SZTU Smart Cancel Planner Hourly
```

检查任务状态：

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like "SZTU Smart*" } | Select-Object TaskName,State
```

检查下次运行时间：

```powershell
Get-ScheduledTaskInfo -TaskName "SZTU Smart Badminton Precheck 1755"
Get-ScheduledTaskInfo -TaskName "SZTU Smart Booking Badminton 18"
```

## 手动预检

羽毛球包场预检：

```powershell
python .\scripts\precheck_badminton.py --config config.json --venue-id 3 --block-type 2 --site-date-type 2 --slots 19:00,20:20 --account-profile badminton
```

预期输出中应包含：

```text
unauthorized=False
expanded_counts={'19:00': 6, '20:20': 6}
```

缓存文件：

```text
state/badminton_package_cache.json
```

## 手动预约

统一入口按 profile 预约：

```powershell
python .\scripts\automation_dispatch.py --config config.json book-profile --profile badminton_18pm
python .\scripts\automation_dispatch.py --config config.json book-profile --profile fitness_center_19pm
python .\scripts\automation_dispatch.py --config config.json book-profile --profile gym_room_19pm
```

羽毛球包场强策略手动命令：

```powershell
python .\scripts\book_api_daily.py --config config.json --wait-until 18:00:00 --slots 19:00,20:20 --venue-id 3 --block-type 2 --site-date-type 2 --poll-interval-ms 25 --preheat-requests 6 --create-retries 5 --multi-session-hold --booking-window-seconds 180 --session-workers 1 --max-holds-per-slot 1 --account-profile badminton --peer-accounts 2310412088 --ntp-sync
```

## 手动取消

最快取消方式是按订单号走纯 API：

```powershell
python .\scripts\cancel_order_api.py --config config.json --order-no SZTUODRxxxx
```

需要推送取消结果时：

```powershell
python .\scripts\cancel_order_api.py --config config.json --order-no SZTUODRxxxx --notify
```

## 自动取消机制

计划任务 `SZTU Smart Cancel Planner Hourly` 每小时运行：

```powershell
python .\scripts\plan_cancel_tasks.py --config config.json --grace-minutes 61
```

它会：

1. 扫描当天订单。
2. 计算 `开场时间 + 61 分钟`。
3. 为每个订单创建一次性 Windows 计划任务。
4. 到点执行 `cancel_order_api.py`。

示例：

```text
19:00 开场 -> 20:01 取消
20:20 开场 -> 21:21 取消
```

## 场馆 ID

当前已通过网页接口确认：

```text
羽毛球：venue_id=3
体能中心：venue_id=46
健身房：venue_id=4
```

羽毛球包场使用：

```text
venue_id=3
block_type=2
site_date_type=2
```

体能中心和健身房使用：

```text
block_type=1
site_date_type=2
```

## 常用文件

```text
config.json                              当前配置
state/storage.json                       登录态
state/storage_badminton.json             羽毛球独立账号登录态
state/badminton_package_cache.json       羽毛球包场具体 sessionId 缓存
state/test_runs/                         测试记录
logs/                                    日志目录
```

## 推送测试

```powershell
python -c "from autoorder.settings import load_settings; from autoorder.notify import send_notification; s=load_settings('config.json'); print(send_notification(s, title='SZTU push test', lines=['hello']))"
```

## 说明

本项目只用于正常预约、支付、取消流程。不要使用高频恶意请求，不要绕过平台规则。
