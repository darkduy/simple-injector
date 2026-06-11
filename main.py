from PyQt6 import QtWidgets
from gui import InjectorApp
import sys

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle('Fusion')
    win = InjectorApp()
    win.show()
    sys.exit(app.exec())
