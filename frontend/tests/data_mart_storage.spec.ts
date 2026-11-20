import { expect, test } from '@playwright/test'

/**
 * 集市存储上线页面 E2E：
 * 读模式展示、主题/统计行类型分流、迁移与回滚、复盘排序与关联统计追溯。
 * 后端为内存存储，用例间通过页面上的迁移/回滚按钮驱动状态，不依赖固定初始计数。
 */
test.describe('集市存储上线页面', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/data_mart_storage')
    await expect(page.getByTestId('read-mode')).toBeVisible()
  })

  test('展示读端模式与行类型来源构成', async ({ page }) => {
    await expect(page.getByTestId('provenance-table')).toBeVisible()
    const mode = await page.getByTestId('read-mode').innerText()
    expect(['legacy', 'new']).toContain(mode)
    await expect(page.getByTestId('topics-table')).toBeVisible()
  })

  test('主题目录与统计记录按行类型分流', async ({ page }) => {
    await page.getByTestId('tab-topics').click()
    const topicRows = page.locator('[data-testid^="topic-row-"]')
    expect(await topicRows.count()).toBeGreaterThanOrEqual(3)
    for (const type of await page.locator('[data-testid^="topic-row-"] td:nth-child(5)').allInnerTexts()) {
      expect(type).toBe('topic')
    }

    await page.getByTestId('tab-statistics').click()
    const statRows = page.locator('[data-testid^="stat-row-"]')
    for (const type of await page.locator('[data-testid^="stat-row-"] td:nth-child(7)').allInnerTexts()) {
      expect(type).toBe('experiment_stat')
    }
    void statRows
  })

  test('迁移与回滚切换读端模式且批次快照可见', async ({ page }) => {
    const before = await page.getByTestId('read-mode').innerText()

    await page.getByTestId('migrate').click()
    await expect(page.getByTestId('read-mode')).toHaveText('new')
    const batchRows = page.locator('[data-testid^="batch-"]')
    expect(await batchRows.count()).toBeGreaterThanOrEqual(1)
    const checksum = await batchRows.first().locator('.checksum').innerText()
    expect(checksum.trim()).toHaveLength(64)

    await page.getByTestId('rollback').click()
    await expect(page.getByTestId('read-mode')).toHaveText('legacy')
    void before
  })

  test('生成复盘报表并打开关联实验统计', async ({ page, request }) => {
    // 通过 API 准备同一业务日期的甲、乙两项实验统计（无数字编号，按实验+日期追溯）
    const businessDate = '2026-08-20'
    const makeStat = (exp: string, sampleDate: string, lift: number, baseRate: number) => ({
      row_type: 'experiment_stat',
      主题编号: `DATA-STAT-${exp}`,
      实验编号: exp,
      业务日期: businessDate,
      样本日期: sampleDate,
      region: 'all',
      channel: 'all',
      控制组样本量: 5000,
      控制组转化人数: Math.round(5000 * baseRate),
      实验组样本量: 5000,
      实验组转化人数: Math.round(5000 * baseRate * (1 + lift)),
      控制组转化率: baseRate,
      实验组转化率: Math.round(baseRate * (1 + lift) * 10000) / 10000,
      绝对提升: Math.round(baseRate * lift * 10000) / 10000,
      相对提升: lift,
      显著性: false,
    })
    await request.post('/api/data_mart/storage/rollback')
    await request.post('/api/data_mart/statistics/legacy-ingest', {
      data: makeStat('EXPE-001', '2026-08-15', 0.0714, 0.07),
    })
    await request.post('/api/data_mart/statistics/legacy-ingest', {
      data: makeStat('EXPE-002', '2026-08-20', 0.1, 0.06),
    })

    // 迁移到新结构，保证统计记录可复盘
    await page.reload()
    await page.getByTestId('migrate').click()
    await expect(page.getByTestId('read-mode')).toHaveText('new')

    const reviewCountBefore = await page.locator('[data-testid^="review-REVIEW-"]').count()
    await page.getByTestId('review-date').fill(businessDate)
    await page.getByTestId('build-review').click()
    await expect.poll(async () =>
      page.locator('[data-testid^="review-REVIEW-"]').count()
    ).toBeGreaterThan(reviewCountBefore)

    const firstReview = page.locator('[data-testid^="review-REVIEW-"]').first()
    await expect(firstReview.getByTestId('review-order')).toContainText('EXPE')
    const reportNo = (await firstReview.getAttribute('data-testid'))?.replace('review-', '') ?? ''
    await page.getByTestId(`open-${reportNo}`).click()

    const linked = page.getByTestId('linked-panel')
    await expect(linked).toBeVisible()
    await expect(linked.locator('[data-testid^="linked-EXPE-"]')).toHaveCount(2)
    // 关联统计可核对样本日期与旧主题编号
    for (const row of await linked.locator('tbody tr').all()) {
      await expect(row.locator('td').nth(2)).toContainText('DATA-STAT-EXPE-')
      await expect(row.locator('td').nth(3)).toMatch(/\d{4}-\d{2}-\d{2}/)
    }
  })
})
