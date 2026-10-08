// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";

import {AuditAnchor} from "../src/AuditAnchor.sol";
import {ClinicRegistry} from "../src/ClinicRegistry.sol";
import {ConsentRegistry} from "../src/ConsentRegistry.sol";

abstract contract BaseTest is Test {
    address internal admin1 = makeAddr("admin1");
    address internal admin2 = makeAddr("admin2");
    address internal admin3 = makeAddr("admin3");
    address internal poster = makeAddr("poster");
    address internal otherPoster = makeAddr("otherPoster");

    bytes32 internal constant CLINIC = bytes32(uint256(0xc11));
    bytes32 internal constant OTHER_CLINIC = bytes32(uint256(0xc12));
    bytes32 internal constant KEY_1 = keccak256("key-1");
    bytes32 internal constant KEY_2 = keccak256("key-2");

    ClinicRegistry internal registry;
    AuditAnchor internal anchor;
    ConsentRegistry internal consent;

    function setUp() public virtual {
        registry = new ClinicRegistry([admin1, admin2, admin3]);
        anchor = new AuditAnchor(registry);
        consent = new ConsentRegistry(registry);
    }

    function _register(bytes32 clinicId, bytes32 keyHash, address clinicPoster) internal {
        vm.prank(admin1);
        registry.registerClinic(clinicId, keyHash, clinicPoster);
        vm.prank(admin2);
        registry.registerClinic(clinicId, keyHash, clinicPoster);
    }
}
