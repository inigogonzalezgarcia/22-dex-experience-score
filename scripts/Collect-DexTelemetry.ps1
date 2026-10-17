<#
.SYNOPSIS
    Collects digital experience telemetry from one Windows device for the last N days and writes it in the
    format dexscore reads: <DEVICE>-events.jsonl (one JSON object per line) and <DEVICE>-device.csv (one row).

.DESCRIPTION
    Read-only. Nothing is changed on the device except the two output files (and a temporary battery report
    that is deleted at the end). Run it as SYSTEM or as a local administrator: the Diagnostics-Performance log
    is only readable by administrators.

    What it reads and from where:
      boot_seconds        Microsoft-Windows-Diagnostics-Performance/Operational, event 100, field BootTime (ms).
      logon_seconds       Microsoft-Windows-GroupPolicy/Operational, event 8001, field PolicyElaspedTimeInSeconds
                          (Microsoft's spelling). This is the time of user Group Policy processing at logon, a
                          proxy for logon time, not the full time to a usable desktop. See the note in the code.
      app_crash           Application log, event 1000 (Application Error): faulting application name.
      app_hang            Application log, event 1002 (Application Hang): hung application name.
      system_crash        System log, event 1001 from BugCheck / WER-SystemErrorReporting (stop code), and
                          event 41 from Kernel-Power only when BugcheckCode is 0 (hard reset or power loss,
                          which writes no 1001), so one blue screen is not counted twice.
      wifi_disconnect     Microsoft-Windows-WLAN-AutoConfig/Operational, event 8003 (disconnected).
      vpn_failure         Application log, RasClient event 20227 (built-in Windows VPN failed to connect).
                          Third-party VPN clients log elsewhere: adapt this block to yours.
      wifi_signal_pct     netsh wlan show interfaces, right now (one sample per run).
      network_latency_ms  Test-Connection to -LatencyHost, right now (median of 4 pings).
      cpu/mem_high_minutes  Only with -SampleMinutes > 0: samples the counters for that many minutes now.
                          Windows keeps no history of CPU or memory pressure, so for a real picture an RMM
                          agent or Endpoint Analytics is the better source.
      device row          model, OS build, RAM, free space on C:, battery health from powercfg /batteryreport
                          (full charge capacity / design capacity).
    Teams call quality is not collected here: it comes from the Teams admin center / Call Quality Dashboard.

    Run it daily (an Intune remediation detection script or an RMM scheduled task) with -Days 1 and append, or
    weekly with -Days 7. Point -OutputFolder at a share or collect the files with your RMM.

    Status: written for this lab; it has not been run against a real fleet. Event field
    names can differ between Windows builds and languages (netsh output in particular is localised).

.EXAMPLE
    .\Collect-DexTelemetry.ps1 -Days 7 -OutputFolder C:\ProgramData\DexTelemetry -Site Madrid -Department Finance
    .\Collect-DexTelemetry.ps1 -Days 1 -OutputFolder \\fileserver\dex$ -LatencyHost intranet.contoso.example -SampleMinutes 5
#>
[CmdletBinding()]
param(
    [int]$Days = 7,
    [string]$OutputFolder = "$env:ProgramData\DexTelemetry",
    [string]$LatencyHost = "8.8.8.8",          # better: something inside your network that answers ping
    [string]$Site = "",                        # from your RMM custom fields or Intune device categories
    [string]$Floor = "",
    [string]$Department = "",
    [switch]$Vip,
    [switch]$IncludeUser,                      # off by default: the user name is personal data
    [int]$SampleMinutes = 0
)

$ErrorActionPreference = "Stop"
$device = $env:COMPUTERNAME.ToUpper()
$since = (Get-Date).AddDays(-$Days)
New-Item -ItemType Directory -Path $OutputFolder -Force | Out-Null
$eventsPath = Join-Path $OutputFolder "$device-events.jsonl"
$devicePath = Join-Path $OutputFolder "$device-device.csv"
$lines = New-Object System.Collections.Generic.List[string]

function Add-Event([datetime]$When, [string]$Type, $Value) {
    $o = [ordered]@{ ts = $When.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"); device = $device; type = $Type; value = $Value }
    $lines.Add(($o | ConvertTo-Json -Compress))
}

function Get-Events([string]$Log, [int[]]$Id, [string]$Provider = $null) {
    $filter = @{ LogName = $Log; Id = $Id; StartTime = $since }
    if ($Provider) { $filter.ProviderName = $Provider }
    try { Get-WinEvent -FilterHashtable $filter -ErrorAction Stop }
    catch [System.Exception] {
        # "No events were found" is an exception in Get-WinEvent; a missing or unreadable log is not fatal either
        if ($_.Exception.Message -notmatch "No events were found") { Write-Verbose "${Log}: $($_.Exception.Message)" }
        @()
    }
}

function Get-Field($Event, [string]$Name) {
    # Named EventData fields are more robust than Properties[n] positions, which change between versions
    $xml = [xml]$Event.ToXml()
    ($xml.Event.EventData.Data | Where-Object { $_.Name -eq $Name } | Select-Object -First 1).'#text'
}

# --- Boot duration: Diagnostics-Performance 100 ------------------------------------------------------------
# BootTime is in milliseconds and covers power-on to a usable desktop, including the post-boot phase.
# Windows does not log event 100 for every boot (fast startup resumes are not full boots).
foreach ($e in Get-Events "Microsoft-Windows-Diagnostics-Performance/Operational" 100) {
    $ms = Get-Field $e "BootTime"
    if ($ms) { Add-Event $e.TimeCreated "boot_seconds" ([math]::Round([double]$ms / 1000)) }
}

# --- Logon duration: Group Policy 8001 ---------------------------------------------------------------------
# Choice made here: event 8001 ("Completed user logon policy processing ... in N seconds") is present on any
# domain or hybrid-joined device and is easy to read. It measures Group Policy processing for the user, not
# the whole logon (profile load, shell start and startup apps come on top). On Entra-only devices without
# Group Policy it does not exist and logon_seconds is simply missing, which dexscore handles.
# Diagnostics-Performance 100 also has a BootUserProfileProcessingTime field; it only covers logons right
# after a boot, so it is not used here.
foreach ($e in Get-Events "Microsoft-Windows-GroupPolicy/Operational" 8001) {
    $s = Get-Field $e "PolicyElaspedTimeInSeconds"
    if ($s) { Add-Event $e.TimeCreated "logon_seconds" ([int]$s) }
}

# --- App crashes and hangs: Application 1000 / 1002 --------------------------------------------------------
foreach ($e in Get-Events "Application" 1000 "Application Error") {
    Add-Event $e.TimeCreated "app_crash" ([string]$e.Properties[0].Value)      # faulting application name
}
foreach ($e in Get-Events "Application" 1002 "Application Hang") {
    Add-Event $e.TimeCreated "app_hang" ([string]$e.Properties[0].Value)       # hung application name
}

# --- System crashes: BugCheck 1001 and Kernel-Power 41 -----------------------------------------------------
foreach ($e in Get-Events "System" 1001) {
    if ($e.ProviderName -notmatch "BugCheck|WER-SystemErrorReporting") { continue }   # other sources reuse 1001
    $code = [string]$e.Properties[0].Value                                         # e.g. 0x0000009f (0x...)
    Add-Event $e.TimeCreated "system_crash" (($code -split " ")[0])
}
foreach ($e in Get-Events "System" 41 "Microsoft-Windows-Kernel-Power") {
    # BugcheckCode 0: the device lost power or was held off with the button while hung. Non-zero codes are
    # already counted by the 1001 event of the same crash.
    if ([int](Get-Field $e "BugcheckCode") -eq 0) { Add-Event $e.TimeCreated "system_crash" "kernel-power-41" }
}

# --- Wi-Fi disconnects: WLAN-AutoConfig 8003 ---------------------------------------------------------------
# Also logged when the user switches network or the laptop sleeps, so this over-counts a little.
foreach ($e in Get-Events "Microsoft-Windows-WLAN-AutoConfig/Operational" 8003) {
    Add-Event $e.TimeCreated "wifi_disconnect" ""
}

# --- VPN failures: RasClient 20227 (built-in Windows VPN) --------------------------------------------------
foreach ($e in Get-Events "Application" 20227 "RasClient") {
    $err = ([regex]::Match($e.Message, "error code returned on failure is (\d+)")).Groups[1].Value
    Add-Event $e.TimeCreated "vpn_failure" $err
}

# --- Wi-Fi signal now: netsh (English output; other languages need a different label) ---------------------
try {
    $netsh = netsh wlan show interfaces 2>$null
    $signal = $netsh | Select-String -Pattern "^\s*Signal\s*:\s*(\d+)%" | Select-Object -First 1
    if ($signal) { Add-Event (Get-Date) "wifi_signal_pct" ([int]$signal.Matches[0].Groups[1].Value) }
} catch { Write-Verbose "netsh: $($_.Exception.Message)" }   # no Wi-Fi adapter: desktops on a cable

# --- Latency now: Test-Connection (works in Windows PowerShell 5.1 and PowerShell 7) -----------------------
try {
    $pings = Test-Connection $LatencyHost -Count 4 -ErrorAction Stop
    $ms = @($pings | ForEach-Object { if ($_.PSObject.Properties['Latency']) { $_.Latency } else { $_.ResponseTime } } |
            Sort-Object)
    if ($ms.Count) { Add-Event (Get-Date) "network_latency_ms" $ms[[int][math]::Floor($ms.Count / 2)] }
} catch { Write-Verbose "Latency to $LatencyHost not measured: $($_.Exception.Message)" }

# --- Optional: CPU and memory pressure, sampled now ---------------------------------------------------------
if ($SampleMinutes -gt 0) {
    $counters = "\Processor(_Total)\% Processor Time", "\Memory\% Committed Bytes In Use"
    $samples = Get-Counter -Counter $counters -SampleInterval 10 -MaxSamples ($SampleMinutes * 6)
    $cpuHigh = @($samples | Where-Object { ($_.CounterSamples | Where-Object Path -like "*processor*").CookedValue -ge 90 }).Count
    $memHigh = @($samples | Where-Object { ($_.CounterSamples | Where-Object Path -like "*committed*").CookedValue -ge 90 }).Count
    # 6 samples = 1 minute
    if ($cpuHigh) { Add-Event (Get-Date) "cpu_high_minutes" ([math]::Round($cpuHigh / 6, 1)) }
    if ($memHigh) { Add-Event (Get-Date) "mem_high_minutes" ([math]::Round($memHigh / 6, 1)) }
}

# --- Device row --------------------------------------------------------------------------------------------
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'"
$battery = ""
if (Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue) {
    $xmlPath = Join-Path $env:TEMP "dex-battery-$([guid]::NewGuid()).xml"
    try {
        powercfg /batteryreport /xml /output $xmlPath | Out-Null
        [xml]$report = Get-Content $xmlPath -Raw
        $b = @($report.BatteryReport.Batteries.Battery)[0]
        if ([double]$b.DesignCapacity -gt 0) {
            $battery = [math]::Round(100 * [double]$b.FullChargeCapacity / [double]$b.DesignCapacity, 1)
        }
    } catch { Write-Verbose "Battery report: $($_.Exception.Message)" }
    finally { Remove-Item $xmlPath -ErrorAction SilentlyContinue }
}
$user = ""
if ($IncludeUser -and $cs.UserName) { $user = ($cs.UserName -split "\\")[-1] }

[pscustomobject]@{
    device = $device; user = $user; department = $Department; site = $Site; floor = $Floor
    model = $cs.Model; os_build = "$($os.CurrentBuild).$($os.UBR)"
    ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB)
    disk_free_pct = if ($disk.Size) { [math]::Round(100 * $disk.FreeSpace / $disk.Size, 1) } else { "" }
    battery_health_pct = $battery
    vip = if ($Vip) { "yes" } else { "no" }
} | Export-Csv -Path $devicePath -NoTypeInformation -Encoding UTF8

# Append, so a daily run builds up the history. dexscore does not remove duplicate events, so keep -Days
# equal to the run interval (daily run, -Days 1).
$lines | Add-Content -Path $eventsPath -Encoding UTF8
Write-Host "Wrote $($lines.Count) events to $eventsPath and the device row to $devicePath."
