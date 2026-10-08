// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AuditAnchor} from "../src/AuditAnchor.sol";
import {BaseTest} from "./Base.t.sol";

contract AuditAnchorTest is BaseTest {
    bytes32 internal constant ROOT_1 = keccak256("root-1");
    bytes32 internal constant ROOT_2 = keccak256("root-2");
    bytes32 internal constant DIGEST = keccak256("sth");

    function setUp() public override {
        super.setUp();
        _register(CLINIC, KEY_1, poster);
        _register(OTHER_CLINIC, KEY_2, otherPoster);
    }

    function test_firstAnchorUsesZeroPrevRoot() public {
        vm.warp(1_800_000_000);
        vm.expectEmit(true, false, false, true);
        emit AuditAnchor.Anchored(CLINIC, 10, ROOT_1, DIGEST, 1_800_000_000);
        vm.prank(poster);
        anchor.anchor(CLINIC, 10, ROOT_1, bytes32(0), DIGEST);

        (uint64 size, bytes32 root) = anchor.latest(CLINIC);
        assertEq(size, 10);
        assertEq(root, ROOT_1);
    }

    function test_chainOfAnchors() public {
        vm.startPrank(poster);
        anchor.anchor(CLINIC, 10, ROOT_1, bytes32(0), DIGEST);
        anchor.anchor(CLINIC, 15, ROOT_2, ROOT_1, DIGEST);
        vm.stopPrank();
        (uint64 size, bytes32 root) = anchor.latest(CLINIC);
        assertEq(size, 15);
        assertEq(root, ROOT_2);
    }

    function test_onlyPosterCanAnchor() public {
        vm.prank(admin1);
        vm.expectRevert(AuditAnchor.NotPoster.selector);
        anchor.anchor(CLINIC, 1, ROOT_1, bytes32(0), DIGEST);

        // The poster of another clinic cannot anchor this one.
        vm.prank(otherPoster);
        vm.expectRevert(AuditAnchor.NotPoster.selector);
        anchor.anchor(CLINIC, 1, ROOT_1, bytes32(0), DIGEST);
    }

    function test_unregisteredClinicCannotBeAnchored() public {
        vm.prank(poster);
        vm.expectRevert(AuditAnchor.NotPoster.selector);
        anchor.anchor(keccak256("unknown"), 1, ROOT_1, bytes32(0), DIGEST);
    }

    function testFuzz_onlyPosterCanAnchor(address caller) public {
        vm.assume(caller != poster);
        vm.prank(caller);
        vm.expectRevert(AuditAnchor.NotPoster.selector);
        anchor.anchor(CLINIC, 1, ROOT_1, bytes32(0), DIGEST);
    }

    function testFuzz_treeSizeMustIncrease(uint64 first, uint64 second) public {
        vm.assume(first > 0 && second <= first);
        vm.startPrank(poster);
        anchor.anchor(CLINIC, first, ROOT_1, bytes32(0), DIGEST);
        vm.expectRevert(
            abi.encodeWithSelector(AuditAnchor.TreeSizeNotIncreasing.selector, first, second)
        );
        anchor.anchor(CLINIC, second, ROOT_2, ROOT_1, DIGEST);
        vm.stopPrank();
    }

    function testFuzz_zeroTreeSizeIsRejected(bytes32 root) public {
        vm.prank(poster);
        vm.expectRevert(abi.encodeWithSelector(AuditAnchor.TreeSizeNotIncreasing.selector, 0, 0));
        anchor.anchor(CLINIC, 0, root, bytes32(0), DIGEST);
    }

    function testFuzz_prevRootMustMatch(bytes32 wrongPrev) public {
        vm.assume(wrongPrev != ROOT_1);
        vm.startPrank(poster);
        anchor.anchor(CLINIC, 10, ROOT_1, bytes32(0), DIGEST);
        vm.expectRevert(
            abi.encodeWithSelector(AuditAnchor.PrevRootMismatch.selector, ROOT_1, wrongPrev)
        );
        anchor.anchor(CLINIC, 11, ROOT_2, wrongPrev, DIGEST);
        vm.stopPrank();
    }

    function testFuzz_firstPrevRootMustBeZero(bytes32 prev) public {
        vm.assume(prev != bytes32(0));
        vm.prank(poster);
        vm.expectRevert(
            abi.encodeWithSelector(AuditAnchor.PrevRootMismatch.selector, bytes32(0), prev)
        );
        anchor.anchor(CLINIC, 1, ROOT_1, prev, DIGEST);
    }

    function testFuzz_increasingSequenceIsAccepted(uint64[8] memory steps) public {
        vm.startPrank(poster);
        uint64 size;
        bytes32 prev;
        for (uint256 i = 0; i < steps.length; i++) {
            uint64 step = (steps[i] % 1000) + 1;
            if (size > type(uint64).max - step) break;
            size += step;
            bytes32 root = keccak256(abi.encode(i, size));
            anchor.anchor(CLINIC, size, root, prev, DIGEST);
            prev = root;
        }
        vm.stopPrank();
        (uint64 latestSize, bytes32 latestRoot) = anchor.latest(CLINIC);
        assertEq(latestSize, size);
        assertEq(latestRoot, prev);
    }

    function test_clinicsAreIndependent() public {
        vm.prank(poster);
        anchor.anchor(CLINIC, 10, ROOT_1, bytes32(0), DIGEST);
        vm.prank(otherPoster);
        anchor.anchor(OTHER_CLINIC, 3, ROOT_2, bytes32(0), DIGEST);
        (uint64 size, bytes32 root) = anchor.latest(OTHER_CLINIC);
        assertEq(size, 3);
        assertEq(root, ROOT_2);
    }

    function test_registryIsExposed() public view {
        assertEq(address(anchor.registry()), address(registry));
    }
}
