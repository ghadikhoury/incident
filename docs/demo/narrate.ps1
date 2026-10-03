param([Parameter(Mandatory=$true)][string]$BuildDir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -LiteralPath (Join-Path $BuildDir 'narration.json') | ConvertFrom-Json
for ($i = 0; $i -lt $items.Count; $i++) {
    $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $speaker.Rate = 1
    $speaker.SetOutputToWaveFile((Join-Path $BuildDir ("voice-{0:d2}.wav" -f $i)))
    $speaker.Speak([string]$items[$i])
    $speaker.Dispose()
}
