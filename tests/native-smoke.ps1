[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$OutputDir,

    [ValidateSet('E2E3', 'E4')]
    [string]$Case = 'E2E3',

    [switch]$Prompt
)

$ErrorActionPreference = 'Stop'

$directory = Get-Item -LiteralPath $OutputDir
if ($directory -isnot [System.IO.DirectoryInfo]) {
    throw "OutputDir must be an existing filesystem directory: $OutputDir"
}
$outputPath = $directory.FullName

if ($Prompt) {
    $invocation = '$orchestrator'
    $scriptArgument = "'" + $PSCommandPath.Replace("'", "''") + "'"
    $outputArgument = "'" + $outputPath.Replace("'", "''") + "'"
    if ($Case -eq 'E2E3') {
        @"
$invocation
Run native smoke E2 and E3 with concurrency 3. Output directory: $outputPath

Start these three independent tasks in separate native Worker contexts. Each may write only its assigned file in the output directory.
- alice: write alice.txt with the single line ALICE.
- bravo: write bravo.txt with the single line BRAVO.
- charlie: write charlie.txt with the single line CHARLIE.

Launch all three before waiting for results. Each writer should wait 10 seconds before writing to make execution overlap observable. Each file may end with one CRLF or LF, or have no trailing newline.

After alice, bravo, and charlie have all succeeded, launch a separate read-only Worker for verify, with all three as its prerequisites.
In the command below, -OutputDir points to the existing artifact directory to read. The checker only reads there. The verifier may inspect the checker source if uncertain. It must run this exact command and return the per-file comparisons and exit code:
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $scriptArgument -OutputDir $outputArgument -Case E2E3

The coordinator delegates the writes and check. Return the structured results by task. Identify host-issued Worker IDs and observed lifecycle events separately from Worker-reported results. Record any gaps in the host evidence.
"@
    }
    else {
        @"
$invocation
Run native smoke E4. Output directory: $outputPath

- fail: an independent read-only Worker passes the line below verbatim as the entire command in one PowerShell shell-tool call. Run it directly in the outer PowerShell shell. The marker and exit code 23 are the expected permanent failure. Preserve both as the reason and do not retry.

Write-Output 'NATIVE_SMOKE_EXPECTED_FAILURE'; exit 23

- blocked: depends on fail succeeding. It would write blocked.txt with the single line BLOCKED if started. Record this task as blocked and leave it unlaunched after fail fails.
- independent: has no dependencies. A separate Worker writes only independent.txt with the single line INDEPENDENT. Launch it exactly once alongside fail.

For the independent artifact check after the run, the verifier must not read Worker transcripts.
In the command below, -OutputDir points to the existing artifact directory to read. The checker only reads there. The verifier may inspect the checker source if uncertain. It must run this exact command and report its output and exit code:
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $scriptArgument -OutputDir $outputArgument -Case E4

The coordinator delegates the work. Return the structured results by task. Identify host-issued Worker IDs, observed lifecycle events, and attempt counts separately from Worker-reported results. Record any gaps in the host evidence.
"@
    }
    return
}

$expected = if ($Case -eq 'E2E3') {
    [ordered]@{ 'alice.txt' = 'ALICE'; 'bravo.txt' = 'BRAVO'; 'charlie.txt' = 'CHARLIE' }
}
else {
    [ordered]@{ 'independent.txt' = 'INDEPENDENT' }
}

$failed = $false
foreach ($entry in $expected.GetEnumerator()) {
    $filePath = Join-Path $outputPath $entry.Key
    try {
        $content = [System.IO.File]::ReadAllText($filePath)
    }
    catch {
        Write-Output "FAIL $($entry.Key): $($_.Exception.GetBaseException().Message)"
        $failed = $true
        continue
    }

    $word = [regex]::Escape($entry.Value)
    if ($content -cnotmatch "\A$word(?:\r?\n)?\z") {
        Write-Output "FAIL $($entry.Key): expected the single line $($entry.Value)"
        $failed = $true
        continue
    }

    Write-Output "PASS $($entry.Key)"
}

if ($Case -eq 'E4') {
    if (Test-Path -LiteralPath (Join-Path $outputPath 'blocked.txt')) {
        Write-Output 'FAIL blocked.txt: unexpected path'
        $failed = $true
    }
    else {
        Write-Output 'PASS blocked.txt absent'
    }
}

if ($failed) {
    exit 1
}
exit 0
