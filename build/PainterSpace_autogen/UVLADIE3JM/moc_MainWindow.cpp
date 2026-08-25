/****************************************************************************
** Meta object code from reading C++ file 'MainWindow.h'
**
** Created by: The Qt Meta Object Compiler version 69 (Qt 6.11.1)
**
** WARNING! All changes made in this file will be lost!
*****************************************************************************/

#include "../../../src/MainWindow.h"
#include <QtCore/qmetatype.h>

#include <QtCore/qtmochelpers.h>

#include <memory>


#include <QtCore/qxptype_traits.h>
#if !defined(Q_MOC_OUTPUT_REVISION)
#error "The header file 'MainWindow.h' doesn't include <QObject>."
#elif Q_MOC_OUTPUT_REVISION != 69
#error "This file was generated using the moc from 6.11.1. It"
#error "cannot be used with the include files from this version of Qt."
#error "(The moc has changed too much.)"
#endif

#ifndef Q_CONSTINIT
#define Q_CONSTINIT
#endif

QT_WARNING_PUSH
QT_WARNING_DISABLE_DEPRECATED
QT_WARNING_DISABLE_GCC("-Wuseless-cast")
namespace {
struct qt_meta_tag_ZN10MainWindowE_t {};
} // unnamed namespace

template <> constexpr inline auto MainWindow::qt_create_metaobjectdata<qt_meta_tag_ZN10MainWindowE_t>()
{
    namespace QMC = QtMocConstants;
    QtMocHelpers::StringRefStorage qt_stringData {
        "MainWindow",
        "selectTool",
        "",
        "brushTool",
        "eraserTool",
        "chooseColor",
        "addHuman",
        "addTree",
        "addMountain",
        "addRock",
        "addLight",
        "addTextBlock",
        "addDowntownTower",
        "addResidentialNeighborhood",
        "addResidentialIntersection",
        "addResidentialAvenue",
        "addOcean",
        "addGrandHarbour",
        "syncLayers",
        "selectionChanged",
        "assetId",
        "updateSelectionScale",
        "scale",
        "updateSelectionText",
        "text",
        "updateTextFont",
        "updateTextStyle",
        "updateTextOrientation",
        "positionCoordinateChanged",
        "value",
        "updateTextBlockDimensions",
        "removeSelectedAsset",
        "newConcept",
        "renderScene",
        "setBackgroundColor",
        "humanExpressionChanged",
        "index",
        "poseJointChanged",
        "jointIndex",
        "poseCoordinateChanged"
    };

    QtMocHelpers::UintData qt_methods {
        // Slot 'selectTool'
        QtMocHelpers::SlotData<void()>(1, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'brushTool'
        QtMocHelpers::SlotData<void()>(3, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'eraserTool'
        QtMocHelpers::SlotData<void()>(4, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'chooseColor'
        QtMocHelpers::SlotData<void()>(5, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addHuman'
        QtMocHelpers::SlotData<void()>(6, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addTree'
        QtMocHelpers::SlotData<void()>(7, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addMountain'
        QtMocHelpers::SlotData<void()>(8, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addRock'
        QtMocHelpers::SlotData<void()>(9, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addLight'
        QtMocHelpers::SlotData<void()>(10, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addTextBlock'
        QtMocHelpers::SlotData<void()>(11, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addDowntownTower'
        QtMocHelpers::SlotData<void()>(12, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addResidentialNeighborhood'
        QtMocHelpers::SlotData<void()>(13, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addResidentialIntersection'
        QtMocHelpers::SlotData<void()>(14, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addResidentialAvenue'
        QtMocHelpers::SlotData<void()>(15, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addOcean'
        QtMocHelpers::SlotData<void()>(16, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'addGrandHarbour'
        QtMocHelpers::SlotData<void()>(17, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'syncLayers'
        QtMocHelpers::SlotData<void()>(18, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'selectionChanged'
        QtMocHelpers::SlotData<void(int)>(19, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Int, 20 },
        }}),
        // Slot 'updateSelectionScale'
        QtMocHelpers::SlotData<void(double)>(21, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Double, 22 },
        }}),
        // Slot 'updateSelectionText'
        QtMocHelpers::SlotData<void(const QString &)>(23, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::QString, 24 },
        }}),
        // Slot 'updateTextFont'
        QtMocHelpers::SlotData<void()>(25, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'updateTextStyle'
        QtMocHelpers::SlotData<void()>(26, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'updateTextOrientation'
        QtMocHelpers::SlotData<void()>(27, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'positionCoordinateChanged'
        QtMocHelpers::SlotData<void(double)>(28, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Double, 29 },
        }}),
        // Slot 'updateTextBlockDimensions'
        QtMocHelpers::SlotData<void(double)>(30, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Double, 29 },
        }}),
        // Slot 'removeSelectedAsset'
        QtMocHelpers::SlotData<void()>(31, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'newConcept'
        QtMocHelpers::SlotData<void()>(32, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'renderScene'
        QtMocHelpers::SlotData<void()>(33, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'setBackgroundColor'
        QtMocHelpers::SlotData<void()>(34, 2, QMC::AccessPrivate, QMetaType::Void),
        // Slot 'humanExpressionChanged'
        QtMocHelpers::SlotData<void(int)>(35, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Int, 36 },
        }}),
        // Slot 'poseJointChanged'
        QtMocHelpers::SlotData<void(int)>(37, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Int, 38 },
        }}),
        // Slot 'poseCoordinateChanged'
        QtMocHelpers::SlotData<void(double)>(39, 2, QMC::AccessPrivate, QMetaType::Void, {{
            { QMetaType::Double, 29 },
        }}),
    };
    QtMocHelpers::UintData qt_properties {
    };
    QtMocHelpers::UintData qt_enums {
    };
    return QtMocHelpers::metaObjectData<MainWindow, qt_meta_tag_ZN10MainWindowE_t>(QMC::MetaObjectFlag{}, qt_stringData,
            qt_methods, qt_properties, qt_enums);
}
Q_CONSTINIT const QMetaObject MainWindow::staticMetaObject = { {
    QMetaObject::SuperData::link<QMainWindow::staticMetaObject>(),
    qt_staticMetaObjectStaticContent<qt_meta_tag_ZN10MainWindowE_t>.stringdata,
    qt_staticMetaObjectStaticContent<qt_meta_tag_ZN10MainWindowE_t>.data,
    qt_static_metacall,
    nullptr,
    qt_staticMetaObjectRelocatingContent<qt_meta_tag_ZN10MainWindowE_t>.metaTypes,
    nullptr
} };

void MainWindow::qt_static_metacall(QObject *_o, QMetaObject::Call _c, int _id, void **_a)
{
    auto *_t = static_cast<MainWindow *>(_o);
    if (_c == QMetaObject::InvokeMetaMethod) {
        switch (_id) {
        case 0: _t->selectTool(); break;
        case 1: _t->brushTool(); break;
        case 2: _t->eraserTool(); break;
        case 3: _t->chooseColor(); break;
        case 4: _t->addHuman(); break;
        case 5: _t->addTree(); break;
        case 6: _t->addMountain(); break;
        case 7: _t->addRock(); break;
        case 8: _t->addLight(); break;
        case 9: _t->addTextBlock(); break;
        case 10: _t->addDowntownTower(); break;
        case 11: _t->addResidentialNeighborhood(); break;
        case 12: _t->addResidentialIntersection(); break;
        case 13: _t->addResidentialAvenue(); break;
        case 14: _t->addOcean(); break;
        case 15: _t->addGrandHarbour(); break;
        case 16: _t->syncLayers(); break;
        case 17: _t->selectionChanged((*reinterpret_cast<std::add_pointer_t<int>>(_a[1]))); break;
        case 18: _t->updateSelectionScale((*reinterpret_cast<std::add_pointer_t<double>>(_a[1]))); break;
        case 19: _t->updateSelectionText((*reinterpret_cast<std::add_pointer_t<QString>>(_a[1]))); break;
        case 20: _t->updateTextFont(); break;
        case 21: _t->updateTextStyle(); break;
        case 22: _t->updateTextOrientation(); break;
        case 23: _t->positionCoordinateChanged((*reinterpret_cast<std::add_pointer_t<double>>(_a[1]))); break;
        case 24: _t->updateTextBlockDimensions((*reinterpret_cast<std::add_pointer_t<double>>(_a[1]))); break;
        case 25: _t->removeSelectedAsset(); break;
        case 26: _t->newConcept(); break;
        case 27: _t->renderScene(); break;
        case 28: _t->setBackgroundColor(); break;
        case 29: _t->humanExpressionChanged((*reinterpret_cast<std::add_pointer_t<int>>(_a[1]))); break;
        case 30: _t->poseJointChanged((*reinterpret_cast<std::add_pointer_t<int>>(_a[1]))); break;
        case 31: _t->poseCoordinateChanged((*reinterpret_cast<std::add_pointer_t<double>>(_a[1]))); break;
        default: ;
        }
    }
}

const QMetaObject *MainWindow::metaObject() const
{
    return QObject::d_ptr->metaObject ? QObject::d_ptr->dynamicMetaObject() : &staticMetaObject;
}

void *MainWindow::qt_metacast(const char *_clname)
{
    if (!_clname) return nullptr;
    if (!strcmp(_clname, qt_staticMetaObjectStaticContent<qt_meta_tag_ZN10MainWindowE_t>.strings))
        return static_cast<void*>(this);
    return QMainWindow::qt_metacast(_clname);
}

int MainWindow::qt_metacall(QMetaObject::Call _c, int _id, void **_a)
{
    _id = QMainWindow::qt_metacall(_c, _id, _a);
    if (_id < 0)
        return _id;
    if (_c == QMetaObject::InvokeMetaMethod) {
        if (_id < 32)
            qt_static_metacall(this, _c, _id, _a);
        _id -= 32;
    }
    if (_c == QMetaObject::RegisterMethodArgumentMetaType) {
        if (_id < 32)
            *reinterpret_cast<QMetaType *>(_a[0]) = QMetaType();
        _id -= 32;
    }
    return _id;
}
QT_WARNING_POP
