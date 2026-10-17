# 注册/卸载「香港身份证放号守候」Windows 计划任务
#
# 重要：脚本必须放在【无中文路径】下。
# 上一版放在 C:\Users\zheng\WorkBuddy\杂事\ 下，Register-ScheduledTask
# 把「杂事」写成了乱码字节 (U+93C9 U+5089 vs 正确的 U+6742 U+4E8B)，
# 任务看着注册成功，实际执行会找不到文件。已改为 C:\Users\zheng\hkid-watch。
#
# 用法:
#   powershell -File setup_task.ps1                    # 默认 00:10 起守 45 分钟
#   powershell -File setup_task.ps1 00:10 45 120 30    # 时刻 分钟 天数 探针间隔
#   powershell -File setup_task.ps1 status
#   powershell -File setup_task.ps1 remove

$ErrorActionPreference = 'Stop'
$Here    = "C:\Users\zheng\hkid-watch"
$Py      = "C:\Users\zheng\.workbuddy\binaries\python\versions\3.13.12\python.exe"
$TaskDaily = "HKID-Quota-Daily"
$TaskProbe = "HKID-Quota-Probe"

if (-not (Test-Path $Here)) { New-Item -ItemType Directory -Force -Path $Here | Out-Null }
if (-not (Test-Path "$Here\hkid_sniper.py")) { Write-Error "缺少 $Here\hkid_sniper.py"; exit 1 }

# 自检：路径必须纯 ASCII，且注册后要能反查回同样的字符串。
# 之前踩过的坑：含中文的路径被 Register-ScheduledTask 写成乱码，
# 任务显示 Ready 但执行时找不到脚本。宁可在这里报错。
if ($Here -notmatch '^[\x20-\x7E]+$') {
    Write-Error "路径含非 ASCII 字符，计划任务会写坏：$Here"
    Write-Error "请把脚本放到纯英文路径下，例如 C:\Users\zheng\hkid-watch"
    exit 1
}

function Remove-Task($name) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "  已卸载 $name"
    }
}

if ($args[0] -eq "remove") {
    Remove-Task $TaskDaily; Remove-Task $TaskProbe; exit 0
}

if ($args[0] -eq "status") {
    foreach ($t in @($TaskDaily, $TaskProbe)) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($task) {
            $i = Get-ScheduledTaskInfo -TaskName $t
            $wd  = $task.Actions[0].WorkingDirectory
            $arg = $task.Actions[0].Arguments
            Write-Host "$t"
            Write-Host "   state  : $($task.State)"
            Write-Host "   next   : $($i.NextRunTime)"
            Write-Host "   last   : $($i.LastRunTime)  result=$($i.LastTaskResult)"
            Write-Host "   workdir: $wd  (exists=$(Test-Path $wd))"
            Write-Host "   args   : $arg"
        } else { Write-Host "$t : 未安装" }
    }
    exit 0
}

$At       = if ($args.Count -ge 1) { $args[0] } else { "00:10" }
$Minutes  = if ($args.Count -ge 2) { [int]$args[1] } else { 45 }
$Within   = if ($args.Count -ge 3) { $args[2] } else { "120" }
$ProbeMin = if ($args.Count -ge 4) { [int]$args[3] } else { 30 }

$User = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }

# ---- 任务1: 每日密集守候 ----
Remove-Task $TaskDaily
$dailyAction = New-ScheduledTaskAction -Execute $Py `
    -Argument "daily --at $At --minutes $Minutes --within $Within" `
    -WorkingDirectory $Here
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $At
$dailySettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskDaily -Action $dailyAction -Trigger $dailyTrigger `
    -Settings $dailySettings -Description "HKID daily quota watch" -User $User | Out-Null
Write-Host "OK  $TaskDaily : 每天 $At 起守 $Minutes 分钟 (范围 $Within 天)"

# ---- 任务2: 全天候探针 ----
Remove-Task $TaskProbe
$probeAction = New-ScheduledTaskAction -Execute $Py `
    -Argument "probe_refresh2.py 24 60" -WorkingDirectory $Here
$probeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $ProbeMin) `
    -RepetitionDuration (New-TimeSpan -Days 2)
$probeSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskProbe -Action $probeAction -Trigger $probeTrigger `
    -Settings $probeSettings -Description "HKID quota refresh prober" -User $User | Out-Null
Write-Host "OK  $TaskProbe : 每 $ProbeMin 分钟记录真实放号时刻"

# ---- 注册后校验：反查工作目录，确认没被写坏 ----
foreach ($t in @($TaskDaily, $TaskProbe)) {
    $reg = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    $wd  = $reg.Actions[0].WorkingDirectory
    $ok  = ($wd -eq $Here)
    Write-Host ("VERIFY {0} : workdir='{1}' match={2}" -f $t, $wd, $ok)
    if (-not $ok) {
        Write-Warning "$t 的路径被写坏了，请检查是否有含中文的旧任务残留"
    }
}

Write-Host ""
Write-Host "立即试跑: Start-ScheduledTask -TaskName $TaskDaily"
Write-Host "看状态  : powershell -File $Here\setup_task.ps1 status"
Write-Host "卸载    : powershell -File $Here\setup_task.ps1 remove"
