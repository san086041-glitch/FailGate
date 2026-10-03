# 真实运行：GitHub Actions 里对公开演示仓库跑 failgate verify（.github/workflows/readme-media.yml）
Output docs/media/out/cli-verify-refuted.en.gif

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 16
Set Width 1240
Set Height 600
Set Padding 24
Set Theme "GitHub Dark"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0
Set WindowBar Colorful

Env FAILGATE_LANG "en"

Type "failgate verify san086041-glitch/failgate-demo#5"
Sleep 600ms
Enter
Wait+Screen@120s /(accepted|refuted|inconclusive|通过验收|驳回|无法判定)/
Sleep 5s
