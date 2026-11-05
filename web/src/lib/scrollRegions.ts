// 가로로 넘쳐 표 안에서 스크롤되는 영역을 키보드로도 쓸 수 있게 한다 (09번 요구사항 11.2, KWCAG 키보드 사용 보장).
// 좁은 화면에서 표는 a11y.css에 따라 문서 대신 표 안에서 가로로 스크롤한다. 표에 링크·버튼이 없으면 키보드로 그 영역에
// 갈 수 없으므로, 흔히 쓰는 '초점을 받는 이름 있는 스크롤 영역'(tabindex=0, role=region, aria-label)으로 만든다.
// AntD Table은 스크롤 영역(div.ant-table-content)에 속성을 줄 방법이 없어, 실제로 넘칠 때만 붙이고 아니면 뗀다
// (넓은 화면에서 Tab 정지점이 늘지 않게). React는 이 속성들을 관리하지 않으므로 다시 그려도 지워지지 않는다.
import { useEffect } from 'react'

const SELECTOR = '.ant-table-content'
const MARK = 'data-scroll-region'

// 이름: 가장 가까운 탭·카드·대화상자·서랍 제목
function nameOf(el: HTMLElement): string {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const tab = p.getAttribute('role') === 'tabpanel' ? p.getAttribute('aria-labelledby') : null
    const title = tab ? document.getElementById(tab)
      : p.matches('.ant-card') ? p.querySelector(':scope > .ant-card-head .ant-card-head-title')
        : p.matches('.ant-modal-content') ? p.querySelector('.ant-modal-title')
          : p.matches('.ant-drawer-content') ? p.querySelector('.ant-drawer-title') : null
    const text = title?.textContent?.trim()
    if (text) return `${text} 표 (가로로 스크롤)`
  }
  return '표 (가로로 스크롤)'
}

function update(el: HTMLElement) {
  if (el.scrollWidth > el.clientWidth + 1) {
    el.setAttribute(MARK, '')
    el.tabIndex = 0
    el.setAttribute('role', 'region')
    el.setAttribute('aria-label', nameOf(el)) // 제목이 바뀌었을 수 있다 (건수 등)
  } else if (el.hasAttribute(MARK)) {
    for (const a of [MARK, 'tabindex', 'role', 'aria-label']) el.removeAttribute(a)
  }
}

export function useScrollableTables() {
  useEffect(() => {
    const watched = new Set<HTMLElement>()
    // 표 영역이나 그 안 표의 크기가 바뀌면 (자료 도착·화면 폭 변경) 다시 판단한다
    const resize = new ResizeObserver((entries) => {
      for (const e of entries) {
        const el = e.target.closest<HTMLElement>(SELECTOR)
        if (el) update(el)
      }
    })
    const watch = (el: HTMLElement, on: boolean) => {
      for (const t of [el, el.firstElementChild]) {
        if (!t) continue
        if (on) resize.observe(t)
        else resize.unobserve(t)
      }
    }
    let frame = 0
    const scan = () => {
      frame = 0
      for (const el of watched) if (!el.isConnected) { watch(el, false); watched.delete(el) } // 닫힌 대화상자 등
      for (const el of document.querySelectorAll<HTMLElement>(SELECTOR)) {
        if (watched.has(el)) continue
        watched.add(el)
        watch(el, true) // 처음 관찰할 때 한 번 불린다
      }
    }
    // 새로 그려진 표(대화상자 포함)를 찾는다. 바뀜이 잦아도 한 프레임에 한 번만
    const added = new MutationObserver(() => {
      if (!frame) frame = requestAnimationFrame(scan)
    })
    added.observe(document.body, { childList: true, subtree: true })
    scan()
    return () => {
      added.disconnect()
      resize.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [])
}
