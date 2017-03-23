cmake_minimum_required(VERSION 3.1)


########################################################################
# useful macros 
########################################################################

MACRO(USRP_PERIPHS_APPEND_SOURCES)
    SET(usrp_periphs_sources ${usrp_periphs_sources} PARENT_SCOPE)
    LIST(APPEND usrp_periphs_sources ${ARGV})
ENDMACRO(USRP_PERIPHS_APPEND_SOURCES)

MACRO(USRP_PERIPHS_APPEND_OBJECTS)
    SET(usrp_periphs_objects ${usrp_periphs_objects} PARENT_SCOPE)
    foreach(arg ${ARGV})
        LIST(APPEND usrp_periphs_objects $<TARGET_OBJECTS:${arg}>)
    endforeach(arg)
    SET(usrp_periphs_objects ${usrp_periphs_objects} PARENT_SCOPE)
ENDMACRO(USRP_PERIPHS_APPEND_OBJECTS)

MACRO(USRP_PERIPHS_ADD_OBJECT name)
  ADD_LIBRARY(${name} OBJECT ${ARGN})
  SET_PROPERTY(TARGET ${name} PROPERTY POSITION_INDEPENDENT_CODE ON)
  USRP_PERIPHS_APPEND_OBJECTS(${name})
ENDMACRO(USRP_PERIPHS_ADD_OBJECT)

########################################################################
# Setup Boost
########################################################################
MESSAGE(STATUS "")
MESSAGE(STATUS "Configuring Boost C++ Libraries...")
SET(BOOST_REQUIRED_COMPONENTS
    chrono
    date_time
    filesystem
    program_options
    python
    regex
    system
    unit_test_framework
    serialization
)
IF(MINGW)
    LIST(APPEND BOOST_REQUIRED_COMPONENTS thread_win32)
ELSE()
    LIST(APPEND BOOST_REQUIRED_COMPONENTS thread)
ENDIF()

IF(UNIX AND NOT BOOST_ROOT AND EXISTS "/usr/lib64")
    LIST(APPEND BOOST_LIBRARYDIR "/usr/lib64") #fedora 64-bit fix
ENDIF(UNIX AND NOT BOOST_ROOT AND EXISTS "/usr/lib64")

IF(MSVC)
    SET(BOOST_ALL_DYN_LINK "${BOOST_ALL_DYN_LINK}" CACHE BOOL "boost enable dynamic linking")
    IF(BOOST_ALL_DYN_LINK)
        ADD_DEFINITIONS(-DBOOST_ALL_DYN_LINK) #setup boost auto-linking in msvc
    ELSE(BOOST_ALL_DYN_LINK)
        SET(BOOST_REQUIRED_COMPONENTS) #empty components list for static link
    ENDIF(BOOST_ALL_DYN_LINK)
ENDIF(MSVC)

SET(Boost_ADDITIONAL_VERSIONS
    "1.46.0" "1.46" "1.47.0" "1.47" "1.48.0" "1.48" "1.48.0" "1.49" "1.50.0" "1.50"
    "1.51.0" "1.51" "1.52.0" "1.52" "1.53.0" "1.53" "1.54.0" "1.54" "1.55.0" "1.55"
    "1.56.0" "1.56" "1.57" "1.57" "1.58" "1.59" "1.60" "1.61"
)
FIND_PACKAGE(Boost 1.53 COMPONENTS ${BOOST_REQUIRED_COMPONENTS})

INCLUDE_DIRECTORIES(${Boost_INCLUDE_DIRS})
LINK_DIRECTORIES(${Boost_LIBRARY_DIRS})

MESSAGE(STATUS "Boost include directories: ${Boost_INCLUDE_DIRS}")
MESSAGE(STATUS "Boost library directories: ${Boost_LIBRARY_DIRS}")
MESSAGE(STATUS "Boost libraries: ${Boost_LIBRARIES}")

########################################################################
# Setup library configuration
########################################################################
SET(CMAKE_CXX_STANDARD 11)
SET(MPM_DEVICE "tests" CACHE STRING "Choose a MPM device to build")
SET_PROPERTY(CACHE MPM_DEVICE PROPERTY STRINGS tests n310)

SET(UHD_HOST_ROOT ${CMAKE_SOURCE_DIR}/../host)

ADD_SUBDIRECTORY(include)
INCLUDE_DIRECTORIES(
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${CMAKE_BINARY_DIR}/include
  ${UHD_HOST_ROOT}/include
  )

ADD_SUBDIRECTORY(lib)
IF(MPM_DEVICE STREQUAL tests)
  ADD_SUBDIRECTORY(tests)
ELSEIF(MPM_DEVICE STREQUAL n310)
  ADD_SUBDIRECTORY(n310)
ENDIF(MPM_DEVICE STREQUAL tests)

MESSAGE("usrp_periphs objects: ${usrp_periphs_objects}")
ADD_LIBRARY(usrp-periphs SHARED ${usrp_periphs_objects})
########################################################################
# Setup Python API
########################################################################

SET(PYTHON_ADDITIONAL_VERSIONS 2.7 3.4 3.5)
FIND_PACKAGE(PythonInterp)
unset(PYTHON_LIBRARY)
unset(PYTHON_EXECUTABLE)
FIND_PACKAGE(PythonLibs)
ADD_SUBDIRECTORY(python)

########################################################################