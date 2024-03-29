# -*- coding: utf-8 -*-


from transifex.resources.formats.qt import LinguistHandler # Qt4 TS files
#from resources.formats.java import JavaPropertiesParser # Java .properties
#from resources.formats.apple import AppleStringsParser # Apple .strings
#from resources.formats.ruby import YamlParser # Ruby On Rails (broken)
#from resources.formats.resx import ResXmlParser # Microsoft .NET (not finished)
from transifex.resources.formats.pofile import POHandler # GNU Gettext .PO/.POT parser
from transifex.resources.formats.joomla import JoomlaINIHandler # GNU Gettext .PO/.POT parser
from transifex.resources.formats.javaproperties import JavaPropertiesHandler # Java .PROPERTIES parser
from transifex.resources.formats.desktop import DesktopHandler
from transifex.resources.formats.strings import AppleStringsHandler
from transifex.resources.formats.xliff import XliffHandler

PARSERS = [
    POHandler,
    LinguistHandler,
    JoomlaINIHandler,
    JavaPropertiesHandler,
    DesktopHandler,
    AppleStringsHandler,
    XliffHandler,
] #, JavaPropertiesParser, AppleStringsParser]
