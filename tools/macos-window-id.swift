#!/usr/bin/env swift
import CoreGraphics
import Foundation

enum Command: String {
    case browser
    case vm
}

func windowList() -> [[String: Any]] {
    return CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
}

func windowNumber(_ window: [String: Any]) -> Int? {
    return window[kCGWindowNumber as String] as? Int
}

func ownerName(_ window: [String: Any]) -> String {
    return window[kCGWindowOwnerName as String] as? String ?? ""
}

func title(_ window: [String: Any]) -> String {
    return window[kCGWindowName as String] as? String ?? ""
}

func layer(_ window: [String: Any]) -> Int {
    return window[kCGWindowLayer as String] as? Int ?? 0
}

func area(_ window: [String: Any]) -> CGFloat {
    let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let width = bounds["Width"] as? CGFloat ?? 0
    let height = bounds["Height"] as? CGFloat ?? 0
    return width * height
}

func findBrowserWindow(appName: String, runId: String) -> Int? {
    let candidates = windowList().compactMap { window -> (number: Int, layer: Int, area: CGFloat)? in
        let owner = ownerName(window)
        guard owner == appName || owner.contains(appName) else { return nil }

        let windowTitle = title(window)
        guard windowTitle.contains("DXMTBench") else { return nil }
        if !runId.isEmpty && !windowTitle.contains(runId) { return nil }

        guard let number = windowNumber(window) else { return nil }
        return (number, layer(window), area(window))
    }

    return candidates.sorted { lhs, rhs in
        if lhs.layer != rhs.layer { return lhs.layer < rhs.layer }
        return lhs.area > rhs.area
    }.first?.number
}

func findVirtualBoxWindow(vmName: String) -> Int? {
    let candidates = windowList().compactMap { window -> (number: Int, area: CGFloat)? in
        guard ownerName(window).contains("VirtualBox") else { return nil }

        let windowTitle = title(window)
        if !vmName.isEmpty && !windowTitle.contains(vmName) { return nil }

        guard let number = windowNumber(window) else { return nil }
        return (number, area(window))
    }

    return candidates.sorted { $0.area > $1.area }.first?.number
}

let args = Array(CommandLine.arguments.dropFirst())
guard let commandName = args.first, let command = Command(rawValue: commandName) else {
    fputs("usage: macos-window-id.swift browser <app-name> [run-id] | vm [vm-name]\n", stderr)
    exit(64)
}

let result: Int?
switch command {
case .browser:
    let appName = args.count > 1 ? args[1] : "Google Chrome"
    let runId = args.count > 2 ? args[2] : ""
    result = findBrowserWindow(appName: appName, runId: runId)
case .vm:
    let vmName = args.count > 1 ? args[1] : ""
    result = findVirtualBoxWindow(vmName: vmName)
}

if let value = result {
    print(value)
}
