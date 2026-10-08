// Speak a script one word at a time with eSpeak-NG (WASM, offline) and stitch
// the words together, so every word's start/end time is known exactly.
// Usage: node tts_words.mjs script.txt out.wav out.words.json
import ESpeakNg from "espeak-ng";
import fs from "fs";

const [scriptPath, wavPath, jsonPath] = process.argv.slice(2);
const text = fs.readFileSync(scriptPath, "utf8").replace(/\s+/g, " ").trim();
const words = text.split(" ");
const RATE = 22050;
const pause = w => (/[.?!]$/.test(w) ? 0.45 : /[,;:]$/.test(w) ? 0.22 : 0.06);

async function speak(word) {
  const e = await ESpeakNg({ arguments: ["-v", "en-us", "-s", "175", "-w", "w.wav", word] });
  const buf = Buffer.from(e.FS.readFile("w.wav"));
  const dataAt = buf.indexOf("data") + 8;
  const pcm = new Int16Array(buf.buffer.slice(buf.byteOffset + dataAt, buf.byteOffset + buf.length));
  // trim silence so the word's timing is its audible part
  const thr = 400;
  let a = 0, b = pcm.length - 1;
  while (a < b && Math.abs(pcm[a]) < thr) a++;
  while (b > a && Math.abs(pcm[b]) < thr) b--;
  return pcm.slice(a, b + 1);
}

const chunks = [], timings = [];
let t = 0.6;
chunks.push(new Int16Array(Math.round(t * RATE)));
for (const w of words) {
  const pcm = await speak(w.replace(/[^\w'.,?!-]/g, ""));
  const dur = pcm.length / RATE;
  timings.push({ text: w, start: +t.toFixed(3), end: +(t + dur).toFixed(3) });
  chunks.push(pcm);
  const gap = pause(w);
  chunks.push(new Int16Array(Math.round(gap * RATE)));
  t += dur + gap;
}
chunks.push(new Int16Array(Math.round(1.0 * RATE)));

const total = chunks.reduce((n, c) => n + c.length, 0);
const out = Buffer.alloc(44 + total * 2);
out.write("RIFF", 0); out.writeUInt32LE(36 + total * 2, 4); out.write("WAVE", 8);
out.write("fmt ", 12); out.writeUInt32LE(16, 16); out.writeUInt16LE(1, 20); out.writeUInt16LE(1, 22);
out.writeUInt32LE(RATE, 24); out.writeUInt32LE(RATE * 2, 28); out.writeUInt16LE(2, 32); out.writeUInt16LE(16, 34);
out.write("data", 36); out.writeUInt32LE(total * 2, 40);
let o = 44;
for (const c of chunks) for (const s of c) { out.writeInt16LE(s, o); o += 2; }
fs.writeFileSync(wavPath, out);
fs.writeFileSync(jsonPath, JSON.stringify({ language: "en", words: timings }, null, 1));
console.log(`${words.length} words, ${(total / RATE).toFixed(2)}s`);
