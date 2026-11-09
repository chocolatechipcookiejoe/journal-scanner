#!/bin/zsh
# Builds "Journal Scanner.app" into ~/Applications (rerun after changing launch.sh).
set -e
HERE="${0:A:h}"
APP="$HOME/Applications/Journal Scanner.app"
WORK=$(mktemp -d)

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# the app starts the scanner from this folder (the one build_app.sh is in)
sed "s|__SCANNER_DIR__|${HERE:h}|" "$HERE/launch.sh" > "$APP/Contents/MacOS/JournalScanner"
chmod +x "$APP/Contents/MacOS/JournalScanner"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Journal Scanner</string>
  <key>CFBundleDisplayName</key><string>Journal Scanner</string>
  <key>CFBundleIdentifier</key><string>local.journalscanner</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>JournalScanner</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF

# Icon: render the SVG with Quick Look, then make an .icns.
if qlmanage -t -s 1024 -o "$WORK" "$HERE/icon.svg" >/dev/null 2>&1; then
  SET="$WORK/AppIcon.iconset"; mkdir "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$WORK/icon.svg.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$WORK/icon.svg.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns"
fi
touch "$APP"
echo "Built $APP"
