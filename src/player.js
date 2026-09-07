// QQ Music web player automation via CDP (puppeteer-core driving real Chrome).
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileP = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const selectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'selectors.json'), 'utf8'));

const CHROME_PATH = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const USER_DATA_DIR = path.join(__dirname, '..', 'data', 'chrome-profile');
const START_URL = 'https://y.qq.com';

let browser = null;
let page = null;

async function firstEl(names) {
  const list = selectors[names];
  if (!list) throw new Error(`Unknown selector group: ${names}`);
  for (const sel of list) {
    try {
      const el = await page.$(sel);
      if (el) return el;
    } catch { /* selector invalid for this page state */ }
  }
  return null;
}

export async function start() {
  if (browser) return;
  browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: false,               // music needs an audible, real page; also easy re-login
    userDataDir: USER_DATA_DIR,    // persists login cookie
    defaultViewport: null,
    args: ['--window-size=1280,900', '--autoplay-policy=no-user-gesture-required', '--no-first-run']
  });
  page = (await browser.pages())[0] || await browser.newPage();
  await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
}

export function isRunning() { return !!browser; }

export async function openLoginPage() {
  if (!page) throw new Error('player not started');
  await page.goto('https://y.qq.com/portal/profile.html', { waitUntil: 'domcontentloaded' }).catch(() => {});
  await page.bringToFront();
}

/** Detect whether the site shows a logged-in avatar (best-effort). */
export async function isLoggedIn() {
  if (!page) return false;
  try {
    const el = await page.$('.top_login__link .login__avatar, .top_login__avatar, img.top_login__cover');
    return !!el;
  } catch { return false; }
}

/** Search a keyword and click the Nth song in results. Returns {title, singer, songMid?}. */
export async function searchAndPlay(keyword, index = 0) {
  if (!page) throw new Error('player not started');
  await page.bringToFront();
  await page.goto(`https://y.qq.com/n/ryqq/search?w=${encodeURIComponent(keyword)}&t=song`, {
    waitUntil: 'domcontentloaded', timeout: 60000
  });
  // wait for result list
  await page.waitForFunction(
    (sels) => sels.some(s => document.querySelectorAll(s).length > 0),
    { timeout: 20000, polling: 500 },
    selectors.searchResultSongItem
  ).catch(() => { throw new Error('搜索结果未出现，请检查网络或更新 selectors.json'); });

  const items = [];
  for (const sel of selectors.searchResultSongItem) {
    const found = await page.$$(sel);
    if (found.length) { items.push(...found); break; }
  }
  const item = items[index];
  if (!item) throw new Error(`搜索结果不足（第 ${index + 1} 首不存在）`);

  const read = async (names) => {
    const el = await item.$(selectors[names][0]).catch(() => null);
    return el ? (await el.evaluate(n => n.textContent.trim())) : '';
  };
  const title = await read('songItemTitle');
  const singer = await read('songItemSinger');

  // middle-click style: open in player bar without leaving the page, fallback to plain click
  await item.$eval('a', a => a.click()).catch(() => item.click());
  await sleep(1500);
  await tryResume();
  return { title: title || keyword, singer: singer || '' };
}

export async function control(action) {
  if (!page) throw new Error('player not started');
  await page.bringToFront();
  const map = { playPause: 'btnPlayPause', next: 'btnNext', prev: 'btnPrev' };
  const el = await firstEl(map[action]);
  if (!el) throw new Error(`找不到播放器按钮：${action}（selectors.json 需更新）`);
  await el.click();
  await sleep(300);
}

async function tryResume() {
  const el = await firstEl('btnPlayPause');
  if (el) await el.click().catch(() => {});
}

/** Current song info from the player bar; empty object when idle. */
export async function nowPlaying() {
  if (!page) return null;
  const title = await textOf('playBarSongName');
  const singer = await textOf('playBarSinger');
  if (!title) return null;
  return { title, singer };
}

async function textOf(names) {
  const el = await firstEl(names);
  return el ? el.evaluate(n => n.textContent.trim()) : '';
}

// ---- system volume (macOS) ----
export async function getSystemVolume() {
  const { stdout } = await execFileP('osascript', ['-e', 'output volume of (get volume settings)']);
  return parseInt(stdout.trim(), 10);
}
export async function setSystemVolume(v) {
  v = Math.max(0, Math.min(100, Math.round(v)));
  await execFileP('osascript', ['-e', `set volume output volume ${v}`]);
  return v;
}

export async function stop() {
  if (browser) { await browser.close().catch(() => {}); browser = null; page = null; }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
