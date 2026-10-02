"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadRenderer() {
  const root = path.join(__dirname, "..", "dist", "static");
  const context = { console };
  vm.createContext(context);
  vm.runInContext(
    fs.readFileSync(path.join(root, "vendor", "marked-15.0.12.min.js"), "utf8"),
    context,
  );
  vm.runInContext(fs.readFileSync(path.join(root, "wizard.js"), "utf8"), context);
  return context.renderMd;
}

test("markdown links work with the packaged Marked version", () => {
  const renderMd = loadRenderer();
  const html = renderMd("[Fulcra](https://fulcradynamics.com)");
  assert.match(html, /href="https:\/\/fulcradynamics\.com"/);
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("markdown cannot insert active HTML or javascript links", () => {
  const renderMd = loadRenderer();
  const html = renderMd(
    '<img src=x onerror="alert(1)"> [bad](javascript:alert(1))',
  );
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /href=/i);
  assert.match(html, /&lt;img/);
  assert.match(html, /bad/);
});
