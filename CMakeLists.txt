cmake_minimum_required(VERSION 3.16)
project(PainterSpace VERSION 0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

set(CMAKE_PREFIX_PATH /Users/vishaljose/Qt/6.11.1/macos)

find_package(Qt6 REQUIRED COMPONENTS Widgets OpenGL OpenGLWidgets)
add_executable(PainterSpace
    src/main.cpp
    src/MainWindow.cpp
    src/MainWindow.h
    src/PaintingCanvas.cpp
    src/PaintingCanvas.h
    assets/Untitled_poseable.glb
    assets/downtown_art_deco_tower.obj
    assets/downtown_art_deco_tower.mtl
    assets/residential_neighborhood.obj
    assets/residential_neighborhood.mtl
    assets/residential_intersection.obj
    assets/residential_intersection.mtl
    assets/residential_avenue.obj
    assets/residential_avenue.mtl
    assets/ocean_horizon.obj
    assets/ocean_horizon.mtl
    assets/grand_harbour.obj
    assets/grand_harbour.mtl
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
    assets/Untitled_poseable.glb
    assets/downtown_art_deco_tower.obj
    assets/downtown_art_deco_tower.mtl
    assets/residential_neighborhood.obj
    assets/residential_neighborhood.mtl
    assets/residential_intersection.obj
    assets/residential_intersection.mtl
    assets/residential_avenue.obj
    assets/residential_avenue.mtl
    assets/ocean_horizon.obj
    assets/ocean_horizon.mtl
    assets/grand_harbour.obj
    assets/grand_harbour.mtl
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

target_link_libraries(PainterSpace PRIVATE
    Qt6::Widgets
    Qt6::OpenGL
    Qt6::OpenGLWidgets
)
