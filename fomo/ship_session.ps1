<#
    ship_session.ps1 -- re-ship the authenticated FOMO Chrome session from borz
    to the VPS and bring fomobot back up.

        cd C:\Users\mzshu\Downloads\memebot\fomo
        powershell -ExecutionPolicy Bypass -File .\ship_session.ps1

    Why this exists: the Chrome profile IS the auth, and Privy rotates its
    refresh token on every use. Whichever machine touched fomo.family last owns
    the session -- so opening fomo.family on borz silently logs the VPS out.
    When that happens the box reports nothing useful ("Failed to fetch"),
    because the API omits access-control-allow-origin on error responses and
    the browser refuses to hand JS the 401. See DEPLOY_VPS.md.

    Only ~4 MB of the 700 MB profile is the session. Cache/ and Code Cache/ are
    dead weight and are deliberately left behind.

    -SkipRemote packages and copies but does not run the remote half.
#>
param(
    [string]$Vps = "root@209.250.245.16",
    [switch]$SkipRemote
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$members = @("Local State", "Default/Local Storage", "Default/IndexedDB",
             "Default/Network", "Default/Preferences")

Write-Host "== 1. nothing may be holding the profile ==" -ForegroundColor Cyan
$holders = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*user-data-dir=*chrome-profile*" -or
                   $_.CommandLine -like "*fomo_bot.py*" })
if ($holders.Count -gt 0) {
    foreach ($p in $holders) { Write-Host ("   PID {0}  {1}" -f $p.ProcessId, $p.Name) }
    Write-Host ""
    Write-Host "   Close the local fomo_bot / the Chrome window using .chrome-profile" -ForegroundColor Yellow
    Write-Host "   first. Copying a live profile ships a half-written leveldb, and two" -ForegroundColor Yellow
    Write-Host "   machines on one Discord token make every interaction fail with 10062." -ForegroundColor Yellow
    exit 1
}
Write-Host "   clean -- no Chrome on the profile, no local fomo_bot.py"

Write-Host "== 2. sanity-check the local session ==" -ForegroundColor Cyan
foreach ($m in $members) {
    $path = Join-Path ".chrome-profile" $m
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host "   MISSING: $path" -ForegroundColor Red
        Write-Host "   This profile has no session to ship. Log in on borz first:" -ForegroundColor Yellow
        Write-Host "       .venv\Scripts\python.exe fomo_browser.py --login" -ForegroundColor Yellow
        exit 1
    }
}
$last = (Get-Item -LiteralPath ".chrome-profile\Default\Preferences").LastWriteTime
Write-Host ("   all five session paths present (profile last used {0:yyyy-MM-dd HH:mm})" -f $last)

Write-Host "== 3. package it ==" -ForegroundColor Cyan
if (Test-Path session.tgz) { Remove-Item session.tgz -Force }
tar -czf session.tgz -C .chrome-profile @members
if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
$mb = (Get-Item session.tgz).Length / 1MB
Write-Host ("   session.tgz = {0:N1} MB" -f $mb)
if ($mb -gt 60) {
    Write-Host "   That is much larger than the usual ~4 MB -- check what got swept in." -ForegroundColor Yellow
}

Write-Host "== 4. ship it ==" -ForegroundColor Cyan
scp session.tgz vps_relogin.sh "${Vps}:/root/"
if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }
Write-Host "   copied to ${Vps}:/root/"

if ($SkipRemote) {
    Write-Host ""
    Write-Host "-SkipRemote: finish on the box with" -ForegroundColor Yellow
    Write-Host "    ssh $Vps 'bash /root/vps_relogin.sh'"
    exit 0
}

Write-Host "== 5. install it on the box ==" -ForegroundColor Cyan
ssh $Vps "bash /root/vps_relogin.sh"
$rc = $LASTEXITCODE
Write-Host ""
if ($rc -eq 0) {
    Write-Host "fomobot is back up. Prove it in Discord: /fomo <handle>" -ForegroundColor Green
    Write-Host "And leave fomo.family closed in this profile from now on --" -ForegroundColor Green
    Write-Host "opening it here revokes the box's session all over again." -ForegroundColor Green
} else {
    Write-Host "The box refused (exit $rc). Read the gate verdict above;" -ForegroundColor Red
    Write-Host "DEPLOY_VPS.md 'If the gate is red' has the fallbacks." -ForegroundColor Red
}
exit $rc
