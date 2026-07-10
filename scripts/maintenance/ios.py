import wda

c = wda.USBClient()

# 1. 打印当前的 XML 树（类似于 Android 的 uiautomator dump）
# 虽然是 XML 字符串，但在控制台里看 text 和 label 已经足够了
print(c.source()) 

# 2. 如果你想看更漂亮的格式，可以把它保存到文件，然后用浏览器的 XML 插件看
with open("ios_tree.xml", "w", encoding="utf-8") as f:
    f.write(c.source())