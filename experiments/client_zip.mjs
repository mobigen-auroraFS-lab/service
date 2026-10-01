// 브라우저가 묶는 방식 모사 — 실제 client-zip 으로 서버의 목록 · 단건 주소를 병렬로 받아 zip 을 디스크에 쓴다.
//   사용: node client_zip.mjs <base> <dir> <병렬 수> <출력 파일>   (마지막 줄에 JSON 한 줄을 낸다)
import { downloadZip } from "client-zip";
import fs from "node:fs";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

const [base, dir, kStr, out] = process.argv.slice(2);
const K = Number(kStr);
const m = await (await fetch(`${base}/x/manifest?dir=${encodeURIComponent(dir)}`)).json();
let requests = 1;
const t0 = performance.now();
let firstByte = null;

async function* inputs() {
  const win = [];
  const start = (it) => { requests++; return fetch(base + it.url).then((r) => ({ name: it.name, input: r })); };
  for (const it of m.items) {
    win.push(start(it));
    if (win.length >= K) yield await win.shift();     // 동시에 K 개를 받아 두고 순서대로 zip 에 넣는다
  }
  while (win.length) yield await win.shift();
}

const resp = downloadZip(inputs());
const src = Readable.fromWeb(resp.body);
src.once("data", () => { firstByte = (performance.now() - t0) / 1000; });
await pipeline(src, fs.createWriteStream(out));
console.log(JSON.stringify({ requests, first_byte_s: firstByte && Number(firstByte.toFixed(3)), total_s: Number(((performance.now() - t0) / 1000).toFixed(2)) }));
