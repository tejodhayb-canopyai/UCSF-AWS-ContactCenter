# ============================================================================
# ROLLBACK: revert GI_Inbound_Main to the pre-"Go ahead" change.
#
# What this restores:
#   - Removes `goAheadMsg=" "` from GI_Reset_Fallback's UpdateContactAttributes
#     (the only block changed by the Go-ahead cleanup on 2026-05-24).
#   - Result: callers hear "Go ahead." before EVERY Q&A turn again
#     (the pre-change behavior).
#
# Effect on traffic:
#   - Applied via aws connect update-contact-flow-content
#   - Takes effect for the NEXT inbound contact (calls in progress finish
#     on the version they started with).
#
# Run from repo root: f:\UCSF-AWS-ContactCenter
# Requires AWS CLI configured for us-east-1, account 642058032951.
# ============================================================================

$snapshot = "_connect_flow_snapshots/GI_Inbound_Main_original_notrail_20260524-204748.json"

if (-not (Test-Path $snapshot)) {
    Write-Host "[ROLLBACK FAILED] Snapshot file not found: $snapshot" -ForegroundColor Red
    Write-Host "Cannot roll back without the pre-change snapshot. Restore it from git history first." -ForegroundColor Red
    exit 1
}

Write-Host "[ROLLBACK] Restoring GI_Inbound_Main to pre-Go-ahead-change snapshot..." -ForegroundColor Yellow
Write-Host "Snapshot: $snapshot ($((Get-Item $snapshot).Length) bytes)"

$result = aws connect update-contact-flow-content `
    --instance-id 0655d3a8-ea38-4bbd-a2e8-79907d12ecad `
    --contact-flow-id 49fa7a14-1ef7-456d-b0ee-32738a62a1be `
    --content file://$snapshot 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ROLLBACK FAILED] $result" -ForegroundColor Red
    exit 1
}

Write-Host "[ROLLBACK OK]" -ForegroundColor Green

# Verify: confirm GI_Reset_Fallback no longer has goAheadMsg.
$current = aws connect describe-contact-flow `
    --instance-id 0655d3a8-ea38-4bbd-a2e8-79907d12ecad `
    --contact-flow-id 49fa7a14-1ef7-456d-b0ee-32738a62a1be `
    --query 'ContactFlow.Content' --output text

$pos = $current.IndexOf('"Identifier":"GI_Reset_Fallback"')
if ($pos -gt 0) {
    $window = $current.Substring([Math]::Max(0, $pos - 120), 250)
    Write-Host ""
    Write-Host "[VERIFY] GI_Reset_Fallback block on the server:" -ForegroundColor Cyan
    Write-Host $window
    if ($window -match 'goAheadMsg') {
        Write-Host ""
        Write-Host "[WARN] goAheadMsg is still in the block. Snapshot may have been wrong." -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "[VERIFY OK] goAheadMsg cleared from GI_Reset_Fallback. Pre-change behavior restored." -ForegroundColor Green
    }
}
