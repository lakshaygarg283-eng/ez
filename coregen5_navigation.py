import re
import time


def navigate(read_screen, send_key, alive, target_mode="local", wait=time.sleep, clock=time.monotonic):
    """
    Navigate gen.exe startup prompts.
    target_mode: "local" | "vpn" | "adb"
    """
    def until(select, seconds=25):
        deadline = clock() + seconds
        while alive() and clock() < deadline:
            result = select([line.lower().strip() for line in read_screen() if line.strip()])
            if result:
                return result
            wait(0.15)
        return None

    def highlighted(lines, label):
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index]
            if label in line and not line.startswith(('>', '❯', '➤', '»')):
                return next((item[1:].strip() for item in lines[index + 1:index + 7]
                             if item.startswith(('>', '❯', '➤', '»'))), None)
        return None

    def at_quantity(lines):
        return lines and re.search(r'how many accounts.*:\s*(\d*)\s*$', lines[-1])

    def first_prompt(lines):
        if at_quantity(lines):
            return 'quantity'
        if 'custom mails' in (highlighted(lines, 'email provider') or ''):
            return 'email'
        return None

    stage = until(first_prompt, 40)
    if stage == 'email':
        send_key('enter')
    if stage not in ('email', 'quantity'):
        return False
    quantity = until(at_quantity)
    if not quantity:
        return False
    for _ in quantity.group(1):
        send_key('backspace')
        wait(0.06)

    for digit in '9999':
        send_key(digit)
        wait(0.06)
    send_key('enter')

    def selected(lines):
        return highlighted(lines, 'connection mode')

    choice = until(selected)
    for _ in range(6):
        if not choice:
            return False
        if target_mode == "adb" and "adb" in choice:
            send_key('enter')
            return True
        elif target_mode == "vpn" and "vpn" in choice:
            send_key('enter')
            return True
        elif target_mode == "local" and ('local ip' in choice or 'original ip' in choice):
            send_key('enter')
            return True
        previous = choice
        send_key('down')
        choice = until(lambda lines: selected(lines) if selected(lines) != previous else None, 3)
    if choice:
        send_key('enter')
        return True
    return False
