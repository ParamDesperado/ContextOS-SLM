# Convert a Word file to PDF with Microsoft Word and print its page count.
param([Parameter(Mandatory)][string]$Docx)
$full = (Resolve-Path $Docx).Path
$pdf = [IO.Path]::ChangeExtension($full, ".pdf")
$word = New-Object -ComObject Word.Application
$word.Visible = $false
try {
    $doc = $word.Documents.Open($full, $false, $true)
    $doc.Repaginate()
    "{0}: {1} pages" -f (Split-Path $full -Leaf), $doc.ComputeStatistics(2)
    $doc.ExportAsFixedFormat($pdf, 17)
    $doc.Close(0)
} finally { $word.Quit() }
