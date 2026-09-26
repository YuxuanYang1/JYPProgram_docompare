import re

INPUT = "duplicates_report.txt"
OUTPUT = "duplicates_report_sorted.txt"

with open(INPUT, encoding='utf-8') as f:
    text = f.read()

# 按 "第 N 组" 切块
blocks = re.split(r'\n(?=第 \d+ 组：)', text)

header = blocks[0]
groups = blocks[1:]

def parse_wasted(block):
    """从 '可省 4.3 MB' 里解析出字节数"""
    m = re.search(r'可省 ([\d.]+) (B|KB|MB|GB|TB)', block)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2)
    factor = {'B':1, 'KB':1024, 'MB':1024**2, 'GB':1024**3, 'TB':1024**4}[unit]
    return val * factor

groups.sort(key=parse_wasted, reverse=True)

with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write(header)
    for i, block in enumerate(groups, 1):
        # 重新编号
        block = re.sub(r'^第 \d+ 组：', f'第 {i} 组：', block)
        f.write(block)

print(f"已重排，输出到 {OUTPUT}")
