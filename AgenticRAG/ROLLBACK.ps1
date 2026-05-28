# ============================================================================
# EMERGENCY ROLLBACK: flip TestBotAlias back to the prod Lambda
# (GIHealthcareLexFulfillment) in both en_US and es_US locales.
#
# When to run this:
#   - Callers report broken / wrong-language / silent responses
#   - CloudWatch shows persistent errors on
#       /aws/lambda/GIHealthcareLexFulfillment_agentic
#   - You just want to revert quickly while we debug
#
# Effect on traffic:
#   - Takes ~10 seconds end-to-end
#   - The next caller to +1 877-427-9082 will hit the prod Lambda
#   - Calls already in progress finish on whichever Lambda they started on
#
# Run from the AgenticRAG/ folder. Requires AWS CLI configured for
# us-east-1, account 642058032951.
# ============================================================================

Write-Host "[ROLLBACK] Flipping TestBotAlias back to GIHealthcareLexFulfillment..." -ForegroundColor Yellow

$result = aws lexv2-models update-bot-alias `
    --bot-id CSMSY7YKWE `
    --bot-alias-id TSTALIASID `
    --bot-alias-name TestBotAlias `
    --bot-version DRAFT `
    --bot-alias-locale-settings file://_rollback_alias_to_prod.json `
    --query "botAliasLocaleSettings" 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ROLLBACK FAILED] $result" -ForegroundColor Red
    Write-Host "Check AWS credentials, then re-run. Or run manually with:" -ForegroundColor Red
    Write-Host "  aws lexv2-models update-bot-alias --bot-id CSMSY7YKWE --bot-alias-id TSTALIASID --bot-alias-name TestBotAlias --bot-version DRAFT --bot-alias-locale-settings file://_rollback_alias_to_prod.json"
    exit 1
}

Write-Host "[ROLLBACK OK]" -ForegroundColor Green
Write-Host $result

# Verify both locales point back at the prod Lambda.
$check = aws lexv2-models describe-bot-alias --bot-id CSMSY7YKWE --bot-alias-id TSTALIASID --query "botAliasLocaleSettings.{en:en_US.codeHookSpecification.lambdaCodeHook.lambdaARN, es:es_US.codeHookSpecification.lambdaCodeHook.lambdaARN}" --output table 2>&1
Write-Host ""
Write-Host "[VERIFY] Current TestBotAlias wiring:" -ForegroundColor Cyan
Write-Host $check
