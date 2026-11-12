// Runs axe-core over the pages written by dump_pages.py. jsdom has no layout, so colour contrast
// is skipped here; the palette is checked by leave/tests/test_accessibility.py instead.
//   npm install jsdom@24 axe-core   (in any scratch folder)   then:   node axe_scan.js /tmp/pages
const fs = require("fs");
const { JSDOM } = require("jsdom");
const axeSource = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

(async () => {
  const dir = process.argv[2] || "/tmp/pages";
  let total = 0;
  for (const file of fs.readdirSync(dir).filter((f) => f.endsWith(".html"))) {
    const dom = new JSDOM(fs.readFileSync(`${dir}/${file}`, "utf8"), { runScripts: "outside-only" });
    dom.window.eval(axeSource);
    const result = await dom.window.axe.run(dom.window.document, { rules: { "color-contrast": { enabled: false } } });
    total += result.violations.length;
    console.log(`${file}: ${result.violations.length} violation(s)`);
    for (const v of result.violations) console.log(`  [${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
  }
  process.exit(total ? 1 : 0);
})();
