import re
import shutil
import subprocess

# copy the required files into repo root
shutil.copy('docs/favicon.ico', '.')
shutil.copy('deploy/windows/instaloader.spec', '.')
shutil.unpack_archive('deploy/windows/ps', '.', 'xztar')
shutil.copy('instaloader/__main__.py', '.')

code = """
import psutil
import subprocess

def __main():
    grandparentpid = psutil.Process(os.getppid()).ppid()
    grandparentpidsearchstring = ' ' + str(grandparentpid) + ' '
    if hasattr(sys, "_MEIPASS"):
        ps = os.path.join(sys._MEIPASS, 'tasklist.exe')
    else:
        ps = 'tasklist'
    popen = subprocess.Popen(ps, stdout=subprocess.PIPE, universal_newlines=True)
    for examine in iter(popen.stdout.readline, ""):
        if grandparentpidsearchstring in examine:
            pname = examine
            break
    popen.stdout.close()
    return_code = popen.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, ps)
    if pname[0:12] == 'explorer.exe':
        subprocess.Popen("cmd /K \"{0}\"".format(os.path.splitext(os.path.basename(sys.argv[0]))[0]))
    else:
        main()


if __name__ == "__main__":
    __main()
"""

with open('__main__.py', 'r+') as f:
    # insert code for magic exe behavior
    f.seek(0, 2)
    f.seek(f.tell() - 42, 0)
    last_lines = f.readlines()
    index = last_lines.index('if __name__ == "__main__":\n')
    f.seek(0, 2)
    f.seek(f.tell() - len(''.join(last_lines[index:])), 0)
    f.writelines(code)

    # adjust imports for changed file structure
    f.seek(0, 0)
    regex = re.compile(r'from (?:(\.[^ ]+ )|\.( ))import')
    lines = [regex.sub(r'from instaloader\1\2import', line) for line in f.readlines()]
    f.seek(0, 0)
    f.writelines(lines)

# install dependencies and invoke PyInstaller
subprocess.Popen("pip install pipenv==2018.11.26").wait()
subprocess.Popen("pipenv sync --dev").wait()
subprocess.Popen("pipenv run pyinstaller instaloader.spec").wait()
subprocess.Popen("dir dist").wait()
