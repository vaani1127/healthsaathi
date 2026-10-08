// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ConsentRegistry} from "../src/ConsentRegistry.sol";
import {BaseTest} from "./Base.t.sol";

contract ConsentRegistryTest is BaseTest {
    bytes32 internal constant HASH = keccak256("salted-consent");

    function setUp() public override {
        super.setUp();
        _register(CLINIC, KEY_1, poster);
    }

    function test_commitAppendsEvents() public {
        vm.warp(1_800_000_000);
        vm.startPrank(poster);
        vm.expectEmit(true, true, false, true);
        emit ConsentRegistry.ConsentCommitted(CLINIC, HASH, 0, 0, 1_800_000_000);
        consent.commit(CLINIC, HASH, consent.GRANTED());
        vm.expectEmit(true, true, false, true);
        emit ConsentRegistry.ConsentCommitted(CLINIC, HASH, 1, 1, 1_800_000_000);
        consent.commit(CLINIC, HASH, consent.WITHDRAWN());
        vm.stopPrank();
        assertEq(consent.commitmentCount(CLINIC), 2);
    }

    function testFuzz_onlyPoster(address caller) public {
        vm.assume(caller != poster);
        vm.prank(caller);
        vm.expectRevert(ConsentRegistry.NotPoster.selector);
        consent.commit(CLINIC, HASH, 0);
    }

    function testFuzz_rejectsUnknownKind(uint8 kind) public {
        vm.assume(kind > 1);
        vm.prank(poster);
        vm.expectRevert(abi.encodeWithSelector(ConsentRegistry.InvalidKind.selector, kind));
        consent.commit(CLINIC, HASH, kind);
    }

    function test_rejectsZeroHash() public {
        vm.prank(poster);
        vm.expectRevert(ConsentRegistry.InvalidHash.selector);
        consent.commit(CLINIC, bytes32(0), 0);
    }
}
