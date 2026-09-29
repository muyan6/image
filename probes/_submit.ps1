<#
  探针提交器 —— 一次运行 = 一个全新账号 + 一次提交
  ================================================
  目标平台新账号赠送 5 鱼干，light 档一次扣 3，
  也就是说一个账号只够跑一张探针。

  这里刻意不做多账号批量注册：一次调用只注册一个账号、只提交一张图，
  跑完就结束，账号密码写在 _submit/account_<OutName>.txt 里可复核。

  用法:
    Set-ExecutionPolicy -Scope Process Bypass -Force
    & .\_submit.ps1 -Probe p8_flat -OutName p8_flat_run1

  产物:
    result\<OutName>.jpg          平台返回图（分析器的输入）
    result\<OutName>__orig.png    平台自己的归一化原图（隔离归一化 vs 增强）
    _submit\*.json                本次会话原始响应
#>
param(
  [Parameter(Mandatory = $true)][string]$Probe,
  [Parameter(Mandatory = $true)][string]$OutName,
  [ValidateSet('light', 'fine')][string]$Quality = 'light'
)

$ErrorActionPreference = 'Stop'

$base   = 'https://save-pic.naix.top'
$work   = Join-Path $PSScriptRoot '_submit'
$resdir = Join-Path $PSScriptRoot 'result'
New-Item -ItemType Directory -Force -Path $work, $resdir | Out-Null

$src = Join-Path $PSScriptRoot "input\$Probe.png"
if (-not (Test-Path $src)) { throw "probe image not found: $src" }

function Read-Json($p) { Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json }
function Ext-Of($p) {
  $b = [System.IO.File]::ReadAllBytes($p)
  if ($b.Length -lt 4) { return '.bin' }
  if ($b[0] -eq 0x89 -and $b[1] -eq 0x50) { return '.png' }
  if ($b[0] -eq 0xFF -and $b[1] -eq 0xD8) { return '.jpg' }
  return '.bin'
}

# ---- 1. 匿名会话，取 CSRF ----
$cj = Join-Path $work "cookies_$OutName.txt"
$s0 = Join-Path $work "session_$OutName.json"
curl.exe -sS --max-time 60 -c $cj "$base/api/auth/session" -o $s0
$csrf = (Read-Json $s0).csrfToken
if (-not $csrf) { throw "no csrfToken: $(Get-Content $s0 -Raw)" }
Write-Output "[1/5] session ok"

# ---- 2. 注册一个新账号（仅一个） ----
$tag  = Get-Date -Format 'MMddHHmmss'
$user = 'p' + $tag + (Get-Random -Minimum 100 -Maximum 999)
$pass = 'Recon' + $tag + 'pass99'

$regPath = Join-Path $work "reg_$OutName.json"
[System.IO.File]::WriteAllText($regPath,
  (@{ username = $user; password = $pass } | ConvertTo-Json -Compress),
  [System.Text.Encoding]::ASCII)

$regOut = Join-Path $work "reg_out_$OutName.json"
curl.exe -sS --max-time 60 -b $cj -c $cj -X POST `
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $csrf" `
  --data-binary "@$regPath" "$base/api/auth/register" -o $regOut

$reg = Read-Json $regOut
if (-not $reg.csrfToken) { throw "register failed: $(Get-Content $regOut -Raw)" }
[System.IO.File]::WriteAllText((Join-Path $work "account_$OutName.txt"),
  "$user`n$pass`n", [System.Text.Encoding]::UTF8)
Write-Output "[2/5] registered $user"

# ---- 3. 提交探针 ----
$rid       = [guid]::NewGuid().ToString()
$rescueOut = Join-Path $work "rescue_$OutName.json"
curl.exe -sS --max-time 120 -b $cj -c $cj -X POST `
  -H "X-CSRF-Token: $($reg.csrfToken)" -H "X-Rescue-Request-Id: $rid" `
  -F "image=@$src" -F "quality=$Quality" -F 'quoteVersion=3' `
  "$base/api/rescue" -o $rescueOut

$rj = Read-Json $rescueOut
if (-not $rj.job) { throw "rescue failed: $(Get-Content $rescueOut -Raw)" }
$jid = $rj.job.id
Write-Output "[3/5] job=$jid  serverSaw=$($rj.job.width)x$($rj.job.height)  cost=$($rj.job.cost)"

# ---- 4. 轮询 ----
$jobOut = Join-Path $work "job_$OutName.json"
$j = $null
for ($i = 0; $i -lt 80; $i++) {
  Start-Sleep -Seconds 3
  curl.exe -sS --max-time 60 -b $cj -c $cj "$base/api/jobs/$jid" -o $jobOut
  $j = Read-Json $jobOut
  if ($j.job.status -ne 'running') { break }
}
$took = if ($j.job.finishedAt) { [math]::Round(($j.job.finishedAt - $j.job.createdAt) / 1000, 1) } else { -1 }
Write-Output "[4/5] status=$($j.job.status)  took=${took}s  err=$($j.job.error)"
if ($j.job.status -ne 'succeeded') { throw "job not succeeded: $($j.job.status)" }

# ---- 5. 下载结果 + 平台归一化原图 ----
$tmp = Join-Path $resdir "$OutName.tmp"
curl.exe -sS --max-time 120 -D (Join-Path $work "hdr_$OutName.txt") -o $tmp -b $cj "$base/api/jobs/$jid/result"
$final = Join-Path $resdir "$OutName$(Ext-Of $tmp)"
Move-Item -Force $tmp $final

$tmp2 = Join-Path $resdir "$OutName`__orig.tmp"
curl.exe -sS --max-time 120 -o $tmp2 -b $cj "$base/api/jobs/$jid/original"
if ((Test-Path $tmp2) -and (Get-Item $tmp2).Length -gt 64) {
  Move-Item -Force $tmp2 (Join-Path $resdir "$OutName`__orig$(Ext-Of $tmp2)")
} else {
  Remove-Item -Force $tmp2 -ErrorAction SilentlyContinue
}

Add-Type -AssemblyName System.Drawing
$im = [System.Drawing.Image]::FromFile($final)
Write-Output "[5/5] saved $final  $($im.Width)x$($im.Height)  $((Get-Item $final).Length) bytes"
$im.Dispose()
