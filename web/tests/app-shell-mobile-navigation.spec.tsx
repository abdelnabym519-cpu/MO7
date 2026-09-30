import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import Link from 'next/link'
import { describe, expect, it, vi } from 'vitest'
import AppShell from '@/components/layout/AppShell'

const device = vi.hoisted(() => ({ isMobile: true }))

vi.mock('@/hooks/useDevice', () => ({
  useDevice: () => ({
    device: device.isMobile ? 'mobile' : 'desktop',
    isMobile: device.isMobile,
    isTablet: false,
    isDesktop: !device.isMobile,
    isCompact: device.isMobile,
  }),
}))

vi.mock('next/navigation', () => ({ usePathname: () => '/chat' }))
vi.mock('next/image', () => ({ default: () => null }))
vi.mock('next/link', () => ({
  default: ({ children, ...props }: { children: React.ReactNode }) => <a {...props}>{children}</a>,
}))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

describe('mobile AppShell navigation', () => {
  it('moves focus into the drawer, traps it, and restores the trigger', async () => {
    render(
      <AppShell
        sidebar={
          <nav aria-label="Primary navigation">
            <Link href="/chat">Chat</Link>
            <Link href="/settings">Settings</Link>
          </nav>
        }
      >
        <h1>Chat</h1>
      </AppShell>
    )

    const trigger = screen.getByRole('button', { name: 'Open navigation' })
    fireEvent.click(trigger)

    const dialog = await screen.findByRole('dialog', {
      name: 'Open navigation',
    })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const chat = screen.getByRole('link', { name: 'Chat' })
    const settings = screen.getByRole('link', { name: 'Settings' })
    await waitFor(() => expect(chat).toHaveFocus())

    settings.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(chat).toHaveFocus()

    chat.focus()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(settings).toHaveFocus()

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(trigger).toHaveFocus()
    })
  })
})
