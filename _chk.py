import py_compile
py_compile.compile("trigger.py", doraise=True)
print("trigger.py OK")
py_compile.compile("memory/card_manager.py", doraise=True)
print("card_manager.py OK")
