import { test, expect } from '@playwright/test';

test.describe('Dashboard Full UI Regression', () => {
  test('all sections and critical buttons are clickable', async ({ page }) => {
    const jsErrors: string[] = [];
    const issues: string[] = [];
    page.on('pageerror', (err) => jsErrors.push(String(err)));

    await page.goto('/');
    await expect(page.locator('body')).toBeVisible();

    const navItems = page.locator('.nav-item[data-tab]');
    const totalNav = await navItems.count();
    expect(totalNav).toBeGreaterThan(0);

    const tabIds: string[] = [];
    for (let i = 0; i < totalNav; i++) {
      const tabId = await navItems.nth(i).getAttribute('data-tab');
      if (tabId) tabIds.push(tabId);
    }
    for (const tabId of tabIds) {
      await page.evaluate((id) => {
        // @ts-ignore
        if (typeof window.showTab === 'function') window.showTab(id);
      }, tabId);
      await page.waitForTimeout(120);
      const section = page.locator(`#${tabId}`);
      const count = await section.count();
      if (count === 0) {
        issues.push(`tab ${tabId}: section not found`);
        continue;
      }
      if (count > 1) {
        issues.push(`tab ${tabId}: duplicate id (${count})`);
      }
      const activeCount = await page.locator(`#${tabId}.active`).count();
      if (activeCount === 0) {
        issues.push(`tab ${tabId}: not active after showTab()`);
      }
    }

    await page.click('.group-header:has-text("База знаний")', { force: true });
    await page.click('.nav-item[data-tab="kb_clients"]', { force: true });
    await page.waitForTimeout(250);

    const openButton = page.locator('button:has-text("ОТКРЫТЬ"):visible').first();
    await expect(openButton).toBeVisible();
    await openButton.click({ force: true });
    await expect(page.locator('#client-panel')).toBeVisible();

    for (const tab of ['general', 'media-plan', 'chat']) {
      await page.evaluate((t) => {
        // @ts-ignore
        if (typeof window.showClientTab === 'function') window.showClientTab(t);
      }, tab);
      await page.waitForTimeout(150);
    }

    const mediaPlanBtn = page.locator('button:has-text("Запустить медиаплан"):visible').first();
    await page.evaluate(() => { if (typeof window.showClientTab === 'function') window.showClientTab('media-plan'); });
    await expect(mediaPlanBtn).toBeVisible();
    await mediaPlanBtn.click({ force: true });
    await page.waitForTimeout(150);

    const sendChatBtn = page.locator('button:has-text("Отправить"):visible').first();
    await page.evaluate(() => { if (typeof window.showClientTab === 'function') window.showClientTab('chat'); });
    await expect(sendChatBtn).toBeVisible();
    await sendChatBtn.click({ force: true });
    await page.waitForTimeout(150);

    await expect(page.locator('.client-tab[data-tab="chat"]')).toContainText('Чат');
    if (jsErrors.length) issues.push(...jsErrors.map((e) => `js: ${e}`));
    expect(issues, `UI issues:\n${issues.join('\n')}`).toEqual([]);
  });
});
