import assert from "node:assert/strict";
import test from "node:test";

import {
  isSalesforceUrl,
  rankSalesforceTabs
} from "../crm-order-dark-mode-extension/salesforce-tabs.mjs";

test("recognizes Salesforce-owned application hosts", () => {
  assert.equal(isSalesforceUrl("https://printfly.lightning.force.com/lightning/r/Account/123/view"), true);
  assert.equal(isSalesforceUrl("https://printfly.my.salesforce.com/001/example"), true);
  assert.equal(isSalesforceUrl("https://printfly--c.visualforce.com/apex/example"), true);
  assert.equal(isSalesforceUrl("https://force.com.evil.example/account"), false);
  assert.equal(isSalesforceUrl("javascript:alert(1)"), false);
});

test("prefers a Salesforce tab in the source window across all windows", () => {
  const ranked = rankSalesforceTabs([
    { id: 10, windowId: 1, active: true, lastAccessed: 500, url: "https://printfly.lightning.force.com/lightning/" },
    { id: 20, windowId: 2, active: false, lastAccessed: 100, url: "https://printfly.my.salesforce.com/001/example" },
    { id: 30, windowId: 2, active: true, lastAccessed: 900, url: "https://example.com/" }
  ], { preferredWindowId: 2 });

  assert.deepEqual(ranked.map((tab) => tab.id), [20, 10]);
});

test("uses the source Salesforce tab and excludes the newly-created target", () => {
  const ranked = rankSalesforceTabs([
    { id: 10, windowId: 1, active: true, lastAccessed: 500, url: "https://printfly.lightning.force.com/lightning/" },
    { id: 11, windowId: 1, active: false, lastAccessed: 600, pendingUrl: "https://printfly.lightning.force.com/001/new" },
    { id: 20, windowId: 2, active: true, lastAccessed: 900, url: "https://login.salesforce.com/" }
  ], { excludeTabId: 11, preferredTabId: 10 });

  assert.deepEqual(ranked.map((tab) => tab.id), [10, 20]);
});
