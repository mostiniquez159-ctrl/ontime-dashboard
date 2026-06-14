import { test, expect } from '@playwright/test';

test.describe('Dashboard V3 E2E', () => {
  test('should open agents and check menu structure', async ({ page }) => {
    await page.goto('/agents');
    const groups = ['Главная', 'Министерства', 'База знаний', 'Воркфлоу', 'Система'];
    for (const group of groups) {
      await expect(page.locator('.group-header').filter({ hasText: group })).toBeVisible();
    }
  });

  test('should verify accordion logic', async ({ page }) => {
    await page.goto('/');
    await page.click('.group-header:has-text("Система")');
    await expect(page.locator('.nav-group[data-group="group_system"]')).toHaveClass(/open/);
    
    await page.click('.group-header:has-text("База знаний")');
    await expect(page.locator('.nav-group[data-group="group_kb"]')).toHaveClass(/open/);
    await expect(page.locator('.nav-group[data-group="group_system"]')).not.toHaveClass(/open/);
  });

  test('should verify client card fields', async ({ page }) => {
    page.on('console', msg => console.log('BROWSER LOG:', msg.text()));
    await page.goto('/');
    await page.click('.group-header:has-text("База знаний")');
    await page.click('.nav-item[data-tab="kb_clients"]');
    await page.locator('.nav-item.active').click(); // Just to be sure
    await page.locator('button.nav-item.active:has-text("ОТКРЫТЬ")').first().click();
    
    const fields = [
        'p-client-id', 'p-project-id', 'p-run-id',
        'p-period', 'p-competitors', 'p-channels',
        'p-product', 'p-segment', 'p-pain',
        'p-offer', 'p-usp', 'p-sheet'
    ];
    for (const id of fields) {
        await expect(page.locator('#' + id)).toBeVisible();
    }
  });
});
