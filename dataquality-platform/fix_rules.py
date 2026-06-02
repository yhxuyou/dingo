
import re

file_path = r"c:\Users\Administrator\Desktop\数据质量检测平台开发\dingo\model\rule\rule_common.py"

# 读取文件
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 定义要替换的模式和替换内容
# 我们需要在每个 def eval(...) 方法的开头，在获取 input_data.content 之后，添加安全检查
# 但首先，让我们用一个更简单的方法：在每个获取 input_data.content 的地方之后，立即添加
# content = input_data.content
# if content is None:
#     content = ""
# elif not isinstance(content, str):
#     content = str(content)
#
# 或者更好的方法，我们可以一次性替换所有相关的模式
#
# 首先，让我们找到所有 "content = input_data.content" 或者类似的行，然后在它们后面添加安全检查

# 模式：匹配 content = input_data.content
pattern1 = r"(content = input_data\.content)(\s+)(if)"
replacement1 = r"\1\2# Ensure content is always a string\n\2if content is None:\n\2    content = \"\"\n\2elif not isinstance(content, str):\n\2    content = str(content)\n\2if"

# 另外，有些规则可能用的是其他变量名，比如 raw_content = input_data.content
# 让我们处理所有类似 "xxx = input_data.content" 的情况

# 先处理 content = input_data.content 的情况
modified_content = re.sub(
    r'(content\s*=\s*input_data\.content)',
    r'''\1
        # Ensure content is always a string
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)''',
    content
)

# 再处理 raw_content = input_data.content 的情况
modified_content = re.sub(
    r'(raw_content\s*=\s*input_data\.content)',
    r'''\1
        # Ensure raw_content is always a string
        if raw_content is None:
            raw_content = ""
        elif not isinstance(raw_content, str):
            raw_content = str(raw_content)''',
    modified_content
)

# 再处理 text = input_data.content 的情况
modified_content = re.sub(
    r'(text\s*=\s*input_data\.content)',
    r'''\1
        # Ensure text is always a string
        if text is None:
            text = ""
        elif not isinstance(text, str):
            text = str(text)''',
    modified_content
)

# 保存修改后的文件
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(modified_content)

print("Rule file fixed successfully!")
