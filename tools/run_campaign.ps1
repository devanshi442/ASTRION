<#
.SYNOPSIS
    ASTRION Bounded Batch Campaign Runner PowerShell Wrapper.
.DESCRIPTION
    Wraps tools\run_campaign.py with default arguments enforcing bounded runs on the staging copy only.
.EXAMPLE
    .\tools\run_campaign.ps1 -MaxHours 3.0 -MaxBatches 10 -MaxSamples 20
#>
param (
    [Parameter(Mandatory=$true)]
    [double]$MaxHours,

    [Parameter(Mandatory=$true)]
    [int]$MaxBatches,

    [Parameter(Mandatory=$true)]
    [int]$MaxSamples,

    [int]$BatchSamples = 5,
    [int]$BatchSeconds = 600,
    [int]$BatchRequests = 100,
    [int]$BatchWorkers = 2,
    [int]$CooldownSeconds = 300,
    [string]$StagingDir = "E:\Downloads\ASTRION\data\dataset_v2_staging_20261011_rec_work"
)

$PythonExe = "E:\Downloads\ASTRION\envs\probe\Scripts\python.exe"
$ScriptPy = "E:\Downloads\ASTRION\tools\run_campaign.py"

& $PythonExe $ScriptPy `
    --max_campaign_hours $MaxHours `
    --max_batches $MaxBatches `
    --max_total_samples $MaxSamples `
    --batch_max_samples $BatchSamples `
    --batch_max_seconds $BatchSeconds `
    --batch_max_requests $BatchRequests `
    --batch_workers $BatchWorkers `
    --cooldown_seconds $CooldownSeconds `
    --staging_dir $StagingDir
