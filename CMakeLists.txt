cmake_minimum_required(VERSION 3.16)
project(PainterSpace VERSION 0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

set(CMAKE_PREFIX_PATH /Users/vishaljose/Qt/6.11.1/macos)

find_package(Qt6 REQUIRED COMPONENTS Widgets OpenGL Network)

if(NOT EXISTS "${CMAKE_SOURCE_DIR}/third_party/openusd/include/pxr/pxr.h")
    execute_process(
        COMMAND /bin/bash "${CMAKE_SOURCE_DIR}/scripts/setup_openusd_sdk.sh"
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    )
endif()

if(CMAKE_OSX_SYSROOT AND EXISTS "${CMAKE_OSX_SYSROOT}/usr/lib/usd/libusd_ms.tbd")
    set(OPENUSD_SYSTEM_LIB "${CMAKE_OSX_SYSROOT}/usr/lib/usd/libusd_ms.tbd")
else()
    set(OPENUSD_SYSTEM_LIB "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/lib/usd/libusd_ms.tbd")
endif()

add_executable(PainterSpace
    src/main.cpp
    src/MainWindow.cpp
    src/MainWindow.h
    src/AssetTypes.h
    src/CameraRig.cpp
    src/CameraRig.h
    src/CameraTools.cpp
    src/CameraTools.h
    src/UsdViewportWidget.cpp
    src/UsdViewportWidget.h
    src/ShotEngine.cpp
    src/ShotEngine.h
    src/ActionTimelineWidget.cpp
    src/ActionTimelineWidget.h
    assets/facial_expressions/README.md
    assets/facial_expressions/happy.obj
    assets/facial_expressions/happy.mtl
    assets/facial_expressions/sad.obj
    assets/facial_expressions/sad.mtl
    assets/facial_expressions/scared.obj
    assets/facial_expressions/scared.mtl
    assets/facial_expressions/grumpy.obj
    assets/facial_expressions/grumpy.mtl
    assets/facial_expressions/terrified.obj
    assets/facial_expressions/terrified.mtl
    assets/facial_expressions/ecstatic.obj
    assets/facial_expressions/ecstatic.mtl
    assets/facial_expressions/angry.obj
    assets/facial_expressions/angry.mtl
    assets/facial_expressions/surprised.obj
    assets/facial_expressions/surprised.mtl
)

set_source_files_properties(
    assets/facial_expressions/README.md
    assets/facial_expressions/happy.obj
    assets/facial_expressions/happy.mtl
    assets/facial_expressions/sad.obj
    assets/facial_expressions/sad.mtl
    assets/facial_expressions/scared.obj
    assets/facial_expressions/scared.mtl
    assets/facial_expressions/grumpy.obj
    assets/facial_expressions/grumpy.mtl
    assets/facial_expressions/terrified.obj
    assets/facial_expressions/terrified.mtl
    assets/facial_expressions/ecstatic.obj
    assets/facial_expressions/ecstatic.mtl
    assets/facial_expressions/angry.obj
    assets/facial_expressions/angry.mtl
    assets/facial_expressions/surprised.obj
    assets/facial_expressions/surprised.mtl
    PROPERTIES HEADER_FILE_ONLY TRUE
)

target_include_directories(PainterSpace PRIVATE
    "${CMAKE_SOURCE_DIR}/third_party/openusd/include"
    "/opt/homebrew/include"
)

target_compile_options(PainterSpace PRIVATE
    -Wno-deprecated-declarations
)

target_link_libraries(PainterSpace PRIVATE
    Qt6::Widgets
    Qt6::OpenGL
    Qt6::Network
    "${OPENUSD_SYSTEM_LIB}"
)
