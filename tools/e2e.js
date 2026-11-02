// End-to-end check on an iPhone 15 (WebKit): page layout, real product links, flight links, taps.
const { webkit, devices } = require("playwright");
const fs = require("fs"), path = require("path");
const ROOT = path.resolve(__dirname, "..");
const RAW = "https://raw.githubusercontent.com/shlomomeisner/deal-alerts/main/";
const TEST_TOPIC = process.env.TEST_TOPIC;
const out = { taps: [], products: [], flights: [], similar: [] };
const shot = (p, n) => p.screenshot({ path: `shots/${n}.png`, timeout: 15000 }).catch(e => console.log("SHOT skipped", n, e.message.slice(0, 80)));
(async () => {
  fs.mkdirSync("shots", { recursive: true });
  const b = await webkit.launch();
  const ctx = await b.newContext({ ...devices["iPhone 15"], locale: "en-US", timezoneId: "America/New_York" });
  const page = await ctx.newPage();
  page.on("pageerror", e => console.log("PAGE ERROR", e.message));
  await page.route(RAW + "**", r => {       // the page reads its data from this run's files
    const f = path.join(ROOT, new URL(r.request().url()).pathname.split("/main/")[1]);
    fs.existsSync(f) ? r.fulfill({ path: f, contentType: "application/json" }) : r.fulfill({ status: 404, body: "" });
  });
  await page.route("https://ntfy.sh/steal-watch-taste-**", r => {   // taps go to a throwaway test topic
    out.taps.push(JSON.parse(r.request().postData()));
    r.continue({ url: "https://ntfy.sh/" + TEST_TOPIC });
  });
  await page.goto("file://" + path.join(ROOT, "index.html"));
  await page.waitForSelector(".deal", { timeout: 30000 });
  await page.waitForTimeout(1500);
  const info = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth, vw: innerWidth,
    cards: document.querySelectorAll(".deal").length, exact: document.querySelectorAll(".deal:not(.nolink)").length,
    firstNolinkIdx: [...document.querySelectorAll("#list > *")].findIndex(e => e.classList.contains("nolink")),
    lastExactIdx: [...document.querySelectorAll("#list > *")].map(e => e.classList.contains("deal") && !e.classList.contains("nolink")).lastIndexOf(true),
    nolinkWithOpen: document.querySelectorAll(".deal.nolink .open").length,
    exactWithoutSimilar: document.querySelectorAll(".deal:not(.nolink):not(:has(.similar))").length,
    stores: document.getElementById("storesLine").textContent,
    spot: (document.querySelector("#spot:not([hidden]) .open") || {}).textContent || "",
  }));
  console.log("PAGE", JSON.stringify(info));
  await shot(page, "01-top");
  const first = await page.$(".deal:not(.nolink)"); await first.scrollIntoViewIfNeeded(); await page.evaluate(() => scrollBy(0, -80)); await shot(page, "02-cards");
  const nl = await page.$(".sect"); if (nl) { await nl.scrollIntoViewIfNeeded(); await shot(page, "03-nolink"); }
  // real product links: one per store, open each like a phone would
  const links = await page.$$eval(".deal:not(.nolink)", els => els.map(e => ({
    title: e.querySelector(".title a").textContent, store: e.querySelector(".store").textContent,
    cat: e.className, href: (e.querySelector(".open") || {}).href, label: (e.querySelector(".open") || {}).textContent,
    similar: (e.querySelector(".similar") || {}).href })).filter(x => x.href && !/travel\/flights/.test(x.href)));
  const per = {};
  for (const l of links) if (!per[l.store]) per[l.store] = l;
  let n = 0;
  for (const l of Object.values(per)) {
    const p = await ctx.newPage();
    let st = 0, body = "", final = "";
    try { const r = await p.goto(l.href, { waitUntil: "domcontentloaded", timeout: 30000 }); st = r ? r.status() : 0; await p.waitForTimeout(3500); final = p.url(); body = (await p.evaluate(() => document.title + " " + document.body.innerText)).replace(/\s+/g, " "); } catch (e) { body = "ERR " + e.message; }
    const words = l.title.toLowerCase().split(/[^a-z0-9]+/).filter(w => w.length > 3);
    const hit = words.filter(w => body.toLowerCase().includes(w)).length;
    const r = { store: l.store, status: st, label: l.label.trim(), href: l.href.slice(0, 120), final: final.slice(0, 120), titleWordsOnPage: `${hit}/${words.length}`, hasPrice: /\$\s?\d/.test(body), search: /search|[?&]q=/.test(final) };
    out.products.push(r); console.log("PRODUCT", JSON.stringify(r));
    if (n++ < 6) await shot(p, "10-product-" + l.store.replace(/\W+/g, "_"));
    await p.close();
  }
  // Touch of Modern in the phone browser: where does a product link really end up?
  const tom = links.find(l => /touchofmodern/.test(l.href));
  if (tom) for (const ua of ["automation-flag", "like-a-real-iphone"]) {
    const c2 = ua === "automation-flag" ? ctx : await b.newContext({ ...devices["iPhone 15"], locale: "en-US", timezoneId: "America/New_York" });
    if (c2 !== ctx) await c2.addInitScript(() => Object.defineProperty(Navigator.prototype, "webdriver", { get: () => false }));
    const p = await c2.newPage(); const nav = [];
    p.on("framenavigated", f => { if (f === p.mainFrame()) nav.push(f.url().slice(0, 100)) });
    try { await p.goto(tom.href, { waitUntil: "domcontentloaded", timeout: 30000 }); await p.waitForTimeout(500); console.log("TOM early title:", await p.title(), "| price on page:", /\$\s?\d/.test(await p.evaluate(() => document.body.innerText))); await shot(p, "11-tom-early-" + ua); await p.waitForTimeout(5000); } catch (e) { }
    console.log("TOM", ua, "navigations:", JSON.stringify(nav), "| final title:", await p.title().catch(() => ""));
    await shot(p, "12-tom-late-" + ua);
    await p.close(); if (c2 !== ctx) await c2.close();
  }
  // a Similar link
  if (links[0] && links[0].similar) { const p = await ctx.newPage(); try { const r = await p.goto(links[0].similar, { waitUntil: "domcontentloaded", timeout: 30000 }); console.log("SIMILAR", r && r.status(), links[0].similar.slice(0, 120)); await p.waitForTimeout(2500); await shot(p, "20-similar"); } catch (e) { console.log("SIMILAR ERR", e.message) } await p.close(); }
  // flights
  if (await page.$("#chip-flights")) {
    await page.click("#chip-flights"); await page.waitForTimeout(800); await page.evaluate(() => scrollTo(0, 0)); await shot(page, "30-flights-tab");
    const fl = await page.$$eval(".deal", els => els.slice(0, 4).map(e => ({ title: e.querySelector(".title a").textContent, dates: (e.querySelector(".fdates") || {}).textContent || "", href: (e.querySelector(".open") || {}).href, label: (e.querySelector(".open") || {}).textContent })));
    let k = 0;
    for (const f of fl) {
      if (!f.href) { console.log("FLIGHT no link", f.title); continue; }
      const p = await ctx.newPage();
      try {
        await p.goto(f.href, { waitUntil: "domcontentloaded", timeout: 45000 });
        await p.waitForFunction(() => /\$\d/.test(document.body.innerText) || /went wrong/.test(document.body.innerText), null, { timeout: 30000 }).catch(() => {}); await p.waitForTimeout(1500);
        const t = (await p.evaluate(() => document.body.innerText)).replace(/\s+/g, " ");
        const r = { deal: f.title.slice(0, 80), dealDates: f.dates.trim().slice(0, 60), prices: (t.match(/\$\d[\d,]*/g) || []).length, cheapest: (t.match(/[Cc]heapest from \$[\d,]+/) || [""])[0], error: /went wrong/.test(t), header: (t.match(/Round trip.{0,160}?Return\w{3}, \w{3} \d+/) || [""])[0] };
        out.flights.push(r); console.log("FLIGHT", JSON.stringify(r));
        await shot(p, "31-flight-" + (k++));
      } catch (e) { console.log("FLIGHT ERR", e.message.slice(0, 150)); }
      await p.close();
    }
    await page.click("#chip-all"); await page.waitForTimeout(500);
  }
  // taps: Keep the first two real deals, ✕ the next one
  const card = i => page.locator("#list .deal:not(.nolink)").nth(i);
  const k1 = await card(0).locator(".title a").textContent(), k2 = await card(1).locator(".title a").textContent(), h1 = await card(2).locator(".title a").textContent();
  await card(0).locator("button.keep").click(); await page.waitForTimeout(600);
  await card(1).locator("button.keep").click(); await page.waitForTimeout(600);
  console.log("KEPT COUNT", await page.locator("#sKept").textContent(), "| kept buttons pressed:", await page.locator('#list button.keep[aria-pressed="true"]').count());
  await page.locator(".deal:not(.nolink)").nth(2).locator("button.hide").click(); await page.waitForTimeout(400); await shot(page, "40-after-hide-toast");
  await page.waitForTimeout(3000);
  console.log("TAPS", JSON.stringify(out.taps.map(t => [t.a, t.t.slice(0, 50)])), "| expected keep:", k1.slice(0, 40), "/", k2.slice(0, 40), "hide:", h1.slice(0, 40));
  // the "Similar only" section at the bottom, and dark mode
  for (let i = 0; i < 25 && !(await page.$(".sect")); i++) { const m = await page.$("button.more"); if (!m) break; await m.click(); await page.waitForTimeout(300); }
  const sect = await page.$(".sect");
  if (sect) { await sect.scrollIntoViewIfNeeded(); await page.evaluate(() => scrollBy(0, -60)); await shot(page, "50-similar-only-section");
    console.log("SECTION", await page.evaluate(() => { const all = [...document.querySelectorAll("#list > *")]; const i = all.findIndex(e => e.classList.contains("sect")); return { sectAt: i, exactAfter: all.slice(i).filter(e => e.classList.contains("deal") && !e.classList.contains("nolink")).length, nolinkBefore: all.slice(0, i).filter(e => e.classList.contains("nolink")).length, nolinkHaveOpen: document.querySelectorAll(".deal.nolink .open").length, nolinkHaveSimilar: document.querySelectorAll(".deal.nolink .similar").length, nolinkTotal: document.querySelectorAll(".deal.nolink").length } })); }
  await page.emulateMedia({ colorScheme: "dark" }); await page.evaluate(() => scrollTo(0, 0)); await page.waitForTimeout(300); await shot(page, "60-dark-top");
  const c4 = page.locator("#list .deal:not(.nolink)").nth(3); await c4.scrollIntoViewIfNeeded(); await shot(page, "61-dark-cards");
  fs.writeFileSync("e2e.json", JSON.stringify(out, null, 1));
  await b.close();
})();
