# Read-only diagnostic through Word's document object model, no UI input or source saves.
param([string]$EvidenceFolder='files/eval/w3/doc-properties')
$ErrorActionPreference='Stop'
$taskEvidence=(Resolve-Path -LiteralPath $EvidenceFolder).Path
$taskSource=(Get-Content -LiteralPath 'files/eval/w3/doc-boundaries/source.json' -Raw -Encoding UTF8 | ConvertFrom-Json).path
$taskWord=$null
$taskProtected=$null
try {
    $taskWord=New-Object -ComObject Word.Application
    # Protected View keeps active content disabled; do not alter Trust Center settings.
    $taskProtected=$taskWord.ProtectedViewWindows.Open($taskSource,$false)
    $taskDocument=$taskProtected.Document
    $taskRange=$taskDocument.Content.Duplicate
    $taskRange.Find.ClearFormatting()
    $taskFound=$taskRange.Find.Execute('容易失误的工作环节')
    if(-not $taskFound){throw 'Word text search did not locate target'}
    $taskTable=$taskRange.Tables.Item(1)
    $taskCells=@()
    foreach($taskCell in $taskTable.Range.Cells){
        $taskCells+=@{row=$taskCell.RowIndex;column=$taskCell.ColumnIndex;start=$taskCell.Range.Start;end=$taskCell.Range.End;text=$taskCell.Range.Text}
    }
    @{application='Microsoft Word';version=$taskWord.Version;protected_view=$true;source_saved=$false;table_start=$taskTable.Range.Start;table_end=$taskTable.Range.End;cells=$taskCells} |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $taskEvidence 'word-reference.json') -Encoding UTF8
    'Word protected-view cell evidence saved'
} finally {
    if($null -ne $taskProtected){$taskProtected.Close()}
    if($null -ne $taskWord){$taskWord.Quit()}
}
