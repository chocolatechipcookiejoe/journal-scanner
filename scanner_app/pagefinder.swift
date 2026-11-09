// pagefinder: find the journal page(s) in a photo, turn them upright, split spreads.
// On-device only (Core Image). No network. The scanner asks the local reading model
// only which of two turns is upright (see "pair").
//
//   pagefinder analyze <photo.jpg>
//       -> {"box": [x, y, w, h], "lines": "across|up-down", "across": a, "updown": b}
//          box = the paper (desk cut away), in photo pixels, top-left origin.
//          lines = which way the handwriting lines run in the photo.
//   pagefinder cut <photo.jpg> <out-dir> <stem> <x> <y> <w> <h> <turn>
//       turn = 0, 90, 180 or 270 degrees clockwise to make the writing upright.
//       Writes <stem>.1.jpg (and <stem>.2.jpg for a spread) and prints
//       {"pages": n, "split_at": fraction or null}
//   pagefinder pair <photo.jpg> <x> <y> <w> <h> <turnA> <turnB> <out.jpg>
//       Both turns side by side (A left, B right). The scanner asks the reading model which one
//       is upright: asked about one image alone it can't tell, but comparing two it can.
import CoreImage
import Foundation
import ImageIO

let ctx = CIContext()
func fail(_ m: String) -> Never { print("{\"error\": \"\(m)\"}"); exit(1) }

func load(_ path: String) -> CIImage {
    guard var im = CIImage(contentsOf: URL(fileURLWithPath: path), options: [.applyOrientationProperty: true]) else { fail("can't open photo") }
    im = im.transformed(by: CGAffineTransform(translationX: -im.extent.origin.x, y: -im.extent.origin.y))
    return im
}

/// Grey pixels, scaled so the long side is `side`. Row 0 is the TOP of the image.
struct Grey { var px: [Float]; var w: Int; var h: Int; var scale: CGFloat
    subscript(x: Int, y: Int) -> Float { px[y * w + x] } }
func grey(_ im: CIImage, side: CGFloat) -> Grey {
    let s = side / max(im.extent.width, im.extent.height)
    let sm = im.transformed(by: CGAffineTransform(scaleX: s, y: s))
    let w = Int(sm.extent.width.rounded(.down)), h = Int(sm.extent.height.rounded(.down))
    var buf = [UInt8](repeating: 0, count: w * h * 4)
    ctx.render(sm, toBitmap: &buf, rowBytes: w * 4, bounds: CGRect(x: 0, y: 0, width: w, height: h),
               format: .RGBA8, colorSpace: CGColorSpaceCreateDeviceRGB())
    var px = [Float](repeating: 0, count: w * h)
    for i in 0..<(w * h) { px[i] = 0.299 * Float(buf[i*4]) + 0.587 * Float(buf[i*4+1]) + 0.114 * Float(buf[i*4+2]) }
    return Grey(px: px, w: w, h: h, scale: s)
}

/// Otsu threshold: the brightness that best separates paper from desk.
func otsu(_ g: Grey) -> Float {
    var hist = [Int](repeating: 0, count: 256)
    for v in g.px { hist[min(255, max(0, Int(v)))] += 1 }
    let total = Float(g.px.count); var sum: Float = 0
    for i in 0..<256 { sum += Float(i * hist[i]) }
    var sumB: Float = 0, wB: Float = 0, best: Float = 0, t: Float = 128
    for i in 0..<256 {
        wB += Float(hist[i]); if wB == 0 { continue }
        let wF = total - wB; if wF == 0 { break }
        sumB += Float(i * hist[i])
        let mB = sumB / wB, mF = (sum - sumB) / wF, between = wB * wF * (mB - mF) * (mB - mF)
        if between > best { best = between; t = Float(i) }
    }
    return t
}

/// Longest run of indices where `frac[i] >= minFrac`.
func run(_ frac: [Float], _ minFrac: Float) -> (Int, Int) {
    var best = (0, frac.count - 1), bestLen = 0, start = -1
    for i in 0...frac.count {
        let on = i < frac.count && frac[i] >= minFrac
        if on && start < 0 { start = i }
        if !on && start >= 0 { if i - start > bestLen { bestLen = i - start; best = (start, i - 1) }; start = -1 }
    }
    return bestLen > 0 ? best : (0, frac.count - 1)
}

/// Pen strokes: pixels clearly darker than their surroundings, on paper.
func inkMap(_ g: Grey) -> [Bool] {
    let r = 6, w = g.w, h = g.h
    var I = [Double](repeating: 0, count: (w + 1) * (h + 1))
    for y in 0..<h { var row = 0.0; for x in 0..<w { row += Double(g[x, y]); I[(y+1)*(w+1) + x+1] = I[y*(w+1) + x+1] + row } }
    let paper = g.px.sorted()[g.px.count * 9 / 10]
    var ink = [Bool](repeating: false, count: w * h)
    for y in 0..<h { for x in 0..<w {
        let x0 = max(0, x-r), x1 = min(w, x+r+1), y0 = max(0, y-r), y1 = min(h, y+r+1)
        let m = (I[y1*(w+1)+x1] - I[y0*(w+1)+x1] - I[y1*(w+1)+x0] + I[y0*(w+1)+x0]) / Double((x1-x0)*(y1-y0))
        ink[y*w + x] = Double(g[x, y]) < m - 10 && Float(m) > paper * 0.7
    } }
    return ink
}

/// How strongly ink forms stripes along this direction: handwriting lines make the
/// ink count per row (for lines running across) rise and fall line by line.
func stripes(_ ink: [Bool], _ w: Int, _ h: Int, across: Bool) -> Double {
    let n = across ? h : w, m = across ? w : h
    var prof = [Double](repeating: 0, count: n)
    for i in 0..<n { var c = 0; for j in 0..<m { if across ? ink[i*w + j] : ink[j*w + i] { c += 1 } }; prof[i] = Double(c) }
    // Keep only line-sized swings: lightly smoothed profile minus a broad moving average.
    // (Big blank areas or two text columns are page-sized swings, not lines.)
    func avg(_ p: [Double], _ k: Int) -> [Double] {
        var out = p, acc = 0.0, q = [Double]()
        for i in 0..<(p.count + k) {
            if i < p.count { acc += p[i]; q.append(p[i]) }
            if q.count > 2 * k + 1 { acc -= q.removeFirst() }
            let c = i - k; if c >= 0 && c < p.count { out[c] = acc / Double(q.count) }
        }
        return out
    }
    let fine = avg(prof, 1), broad = avg(prof, 20)
    var resid = 0.0, level = 0.0
    for i in 0..<n { resid += abs(fine[i] - broad[i]); level += broad[i] }
    return level < 1 ? 0 : resid / level
}

let a = CommandLine.arguments
guard a.count >= 3 else { fail("usage: pagefinder analyze <photo> | cut <photo> <dir> <stem> <x> <y> <w> <h> <turn>") }
let im = load(a[2])
let W = im.extent.width, H = im.extent.height

if a[1] == "analyze" {
    // 1. Paper: rows and columns that are mostly brighter than the desk.
    let g = grey(im, side: 500)
    let t = otsu(g)
    var rowF = [Float](repeating: 0, count: g.h), colF = [Float](repeating: 0, count: g.w)
    for y in 0..<g.h { for x in 0..<g.w where g[x, y] > t { rowF[y] += 1; colF[x] += 1 } }
    for y in 0..<g.h { rowF[y] /= Float(g.w) }; for x in 0..<g.w { colF[x] /= Float(g.h) }
    var (y0, y1) = run(rowF, 0.2), (x0, x1) = run(colF, 0.2)
    let mx = max(2, g.w / 100), my = max(2, g.h / 100)   // a little margin so no writing is shaved off
    x0 = max(0, x0 - mx); x1 = min(g.w - 1, x1 + mx); y0 = max(0, y0 - my); y1 = min(g.h - 1, y1 + my)
    let s = g.scale
    let box = [CGFloat(x0) / s, CGFloat(y0) / s, CGFloat(x1 - x0 + 1) / s, CGFloat(y1 - y0 + 1) / s].map { Int($0.rounded()) }
    // 2. Which way do the handwriting lines run inside the paper?
    let ciBox = CGRect(x: CGFloat(box[0]), y: H - CGFloat(box[1] + box[3]), width: CGFloat(box[2]), height: CGFloat(box[3]))
    let pg = grey(im.cropped(to: ciBox), side: 600)
    let ink = inkMap(pg)
    let across = stripes(ink, pg.w, pg.h, across: true), updown = stripes(ink, pg.w, pg.h, across: false)
    print("{\"box\": [\(box[0]), \(box[1]), \(box[2]), \(box[3])], \"lines\": \"\(updown > across * 1.1 ? "up-down" : "across")\", \"across\": \(String(format: "%.3f", across)), \"updown\": \(String(format: "%.3f", updown))}")
    exit(0)
}

func turned(_ im: CIImage, _ bx: Double, _ by: Double, _ bw: Double, _ bh: Double, _ turn: Int) -> CIImage {
    var page = im.cropped(to: CGRect(x: bx, y: Double(H) - by - bh, width: bw, height: bh))
    page = page.transformed(by: CGAffineTransform(translationX: -page.extent.origin.x, y: -page.extent.origin.y))
    if turn % 360 != 0 {
        page = page.transformed(by: CGAffineTransform(rotationAngle: -CGFloat(turn) * .pi / 180))
        page = page.transformed(by: CGAffineTransform(translationX: -page.extent.origin.x, y: -page.extent.origin.y))
    }
    return page
}

if a[1] == "pair" {
    // pagefinder pair <photo> <x> <y> <w> <h> <turnA> <turnB> <out.jpg>: both turns side by side, A left, B right
    guard a.count == 10, let bx = Double(a[3]), let by = Double(a[4]), let bw = Double(a[5]), let bh = Double(a[6]),
          let ta = Int(a[7]), let tb = Int(a[8]) else { fail("bad arguments") }
    let A = turned(im, bx, by, bw, bh, ta), B = turned(im, bx, by, bw, bh, tb)
    let gap: CGFloat = A.extent.width * 0.06
    let bg = CIImage(color: CIColor(red: 0.1, green: 0.1, blue: 0.1)).cropped(to: CGRect(x: 0, y: 0, width: A.extent.width * 2 + gap, height: A.extent.height))
    let both = B.transformed(by: CGAffineTransform(translationX: A.extent.width + gap, y: 0)).composited(over: A.composited(over: bg))
    try ctx.writeJPEGRepresentation(of: both, to: URL(fileURLWithPath: a[9]), colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                                    options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.85])
    print("{\"ok\": true}"); exit(0)
}

guard a[1] == "cut", a.count == 10, let bx = Double(a[5]), let by = Double(a[6]), let bw = Double(a[7]), let bh = Double(a[8]), let turn = Int(a[9]) else { fail("bad arguments") }
let outDir = URL(fileURLWithPath: a[3]), stem = a[4]
// crop (box is top-left origin; Core Image is bottom-left), then turn clockwise by `turn` degrees
let page = turned(im, bx, by, bw, bh, turn)
let pw = page.extent.width, ph = page.extent.height
var parts = [page]
var splitAt: String = "null"
// 3. Upright and wider than tall: two facing pages. Split at the writing-free strip near the middle.
if pw / ph > 1.15 {
    let g = grey(page, side: 600)
    let ink = inkMap(g)
    var col = [Double](repeating: 0, count: g.w)
    for x in 0..<g.w { var c = 0; for y in 0..<g.h where ink[y*g.w + x] { c += 1 }; col[x] = Double(c) }
    var bestX = g.w / 2, bestV = Double.infinity
    for x in Int(Double(g.w) * 0.38)...Int(Double(g.w) * 0.62) {
        var v = 0.0; for k in max(0, x-4)...min(g.w-1, x+4) { v += col[k] }   // 9-wide strip
        if v < bestV || (v == bestV && abs(x - g.w/2) < abs(bestX - g.w/2)) { bestV = v; bestX = x }
    }
    let cut = pw * CGFloat(bestX) / CGFloat(g.w)
    parts = [page.cropped(to: CGRect(x: 0, y: 0, width: cut, height: ph)),
             page.cropped(to: CGRect(x: cut, y: 0, width: pw - cut, height: ph))]
    splitAt = String(format: "%.2f", Double(bestX) / Double(g.w))
}
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
let cs = CGColorSpace(name: CGColorSpace.sRGB)!
for (i, p) in parts.enumerated() {
    let moved = p.transformed(by: CGAffineTransform(translationX: -p.extent.origin.x, y: -p.extent.origin.y))
    try ctx.writeJPEGRepresentation(of: moved, to: outDir.appendingPathComponent("\(stem).\(i + 1).jpg"), colorSpace: cs,
                                    options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.9])
}
print("{\"pages\": \(parts.count), \"split_at\": \(splitAt)}")
